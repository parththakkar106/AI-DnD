"""Phase 9: abuse guards for hosted, multi-user deployments.

Rate limits and row caps do nothing in local mode, because a single local player
should never be throttled by their own app. The values are hardcoded on purpose.
They are generous enough that a legitimate player never notices them, and tight
enough that a hostile visitor cannot exhaust the demo key, saturate the CPU, or
fill the database.
"""

import json
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import auth, models

# ---------- Rate limiting ----------
# Fixed windows per scope and caller. The windows live in memory, which is
# enough for the single-process deployment this app targets. The worst case
# after a restart is a brief extra allowance.

# Maps a scope to (max requests, window seconds).
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "turn": (10, 60),             # AI turn generation. The demo key also has a daily cap.
    "chat": (30, 60),             # The AI Chat scratchpad, for power users.
    "script-test": (30, 60),      # Sandboxed, but each run costs up to 2s of CPU.
    "connection-test": (10, 60),  # Outbound HTTP to a user-supplied URL.
    "import": (30, 60),           # Large writes.
    "auth": (10, 300),            # Register and login attempts, per IP.
    "guest": (30, 300),           # New guest users, per IP. Each one is a database row.
    # Pageview beacons. The limit is generous, because a real reader clicking
    # around a SPA sends a handful a minute, and it is low enough that nobody
    # can inflate the traffic numbers faster than by reloading the page.
    "analytics": (120, 60),
}

_windows: dict[tuple[str, str], deque] = defaultdict(deque)
_windows_guard = threading.Lock()


# How many proxy hops sit between the app and the real client. On Render, and on
# most platforms, that is one, because the platform's edge appends the connecting
# IP to the right of `X-Forwarded-For`. A client can prepend any value on the
# left, but it cannot push a value past the edge's own append, so the trustworthy
# client IP is the entry that many places from the right rather than uvicorn's
# leftmost choice. Trusting the leftmost entry let anyone rotate
# `X-Forwarded-For` to get a fresh rate-limit bucket per request and bypass the
# auth and guest limits. If the deployment adds more hops, set
# `AIDND_TRUSTED_PROXY_HOPS`.
TRUSTED_PROXY_HOPS = max(1, int(os.environ.get("AIDND_TRUSTED_PROXY_HOPS", "1") or 1))


def client_ip(request: Request) -> str:
    """Returns the real client IP, resisting a spoofed `X-Forwarded-For`.

    The function reads the hop the trusted edge appended, which is the rightmost
    entry minus any extra trusted hops. If no forwarded header is present, which
    happens locally, in development, and on a direct connection, it falls back to
    the socket peer.

    The function is public because the access log needs the same answer. Two
    functions that each decide which address belongs to the caller is how one of
    them ends up trusting a header it should not.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-min(TRUSTED_PROXY_HOPS, len(parts))]
    return request.client.host if request.client else "unknown"


def rate_limit(scope: str, request: Request, user: models.User | None = None) -> None:
    """Raises a 429 when the caller exceeds the scope's window.

    The window is keyed per user when a user is known, because an account
    survives an IP change, and per IP otherwise.
    """
    if not auth.MULTI_USER:
        return
    limit, window_seconds = RATE_LIMITS[scope]
    key = (scope, f"u{user.id}" if user else f"ip{client_ip(request)}")
    now = time.time()
    with _windows_guard:
        window = _windows[key]
        while window and window[0] < now - window_seconds:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(
                429, "You're doing that too fast — wait a minute and try again."
            )
        window.append(now)
        if len(_windows) > 10_000:
            _prune(now)


# ---------- Per-account login throttle ----------
# This is defense in depth next to the per-IP `auth` limit. A botnet dilutes
# that limit, because many real source IPs each get their own bucket, so it
# cannot by itself stop a distributed guessing run against one account. This cap
# keys on the target email rather than on the caller, so guessing one account's
# password stays expensive however many addresses the guesses come from.
#
# Only failures count, and a correct password clears the record. The window
# slides over a short period rather than locking the account, so a user who
# mistypes a few times recovers within minutes. The trade-off is that an
# attacker can keep a known account throttled, which is an inconvenience and is
# preferable to letting the account be brute-forced.
LOGIN_FAIL_LIMIT = 8          # Failed attempts per account.
LOGIN_FAIL_WINDOW = 900       # The window in seconds, which is 15 minutes.

_login_fails: dict[str, deque] = defaultdict(deque)
_login_guard = threading.Lock()


def check_login_allowed(email: str) -> None:
    """Raises a 429 when an account has too many recent failed logins.

    Call this before verifying the password, so that a guess never reaches the
    hash.
    """
    if not auth.MULTI_USER:
        return
    now = time.time()
    with _login_guard:
        window = _login_fails[email]
        while window and window[0] < now - LOGIN_FAIL_WINDOW:
            window.popleft()
        if len(window) >= LOGIN_FAIL_LIMIT:
            raise HTTPException(
                429,
                "Too many failed sign-in attempts for this account — "
                "wait a few minutes and try again.",
            )


def note_login_failure(email: str) -> None:
    """Records one failed attempt against `email`."""
    if not auth.MULTI_USER:
        return
    now = time.time()
    with _login_guard:
        _login_fails[email].append(now)
        if len(_login_fails) > 10_000:  # Bound the map against a flood of unique emails.
            stale = [
                key for key, window in _login_fails.items()
                if not window or window[-1] < now - LOGIN_FAIL_WINDOW
            ]
            for key in stale:
                del _login_fails[key]


def note_login_success(email: str) -> None:
    """Clears the account's failure record after a correct password."""
    with _login_guard:
        _login_fails.pop(email, None)


def _prune(now: float) -> None:
    """Drops callers whose whole window has expired, so the per-IP dict stays bounded.

    Call this with the guard held.
    """
    longest = max(seconds for _, seconds in RATE_LIMITS.values())
    stale = [key for key, window in _windows.items()
             if not window or window[-1] < now - longest]
    for key in stale:
        del _windows[key]


# ---------- Per-user row caps ----------

MAX_ADVENTURES_PER_USER = 100
MAX_SCENARIOS_PER_USER = 200
MAX_SCRIPTS_PER_USER = 200
MAX_STORY_CARDS_PER_OWNER = 200   # Per scenario or per adventure.
MAX_MEMORIES_PER_ADVENTURE = 1000
MAX_ACTIONS_PER_ADVENTURE = 5000
# Phase 14, SP6. A tree holds one branch per divergence somebody built a story
# on, so a tree with more branches than the story has turns came from a file
# rather than from play. The cap applies to imports only. Forking is a POST that
# adds one row and has no cap of its own, and the cap that matters there is
# `MAX_ACTIONS_PER_ADVENTURE` above.
MAX_BRANCHES_PER_ADVENTURE = 1000


def check_row_cap(
    kind: str,
    db: Session,
    user: models.User,
    *,
    adventure: models.Adventure | None = None,
    scenario_id: int | None = None,
    adventure_id: int | None = None,
) -> None:
    """Raises a 409 when creating one more row of `kind` would exceed its cap.

    The caller has already checked ownership of the scenario or adventure passed
    in.
    """
    if not auth.MULTI_USER:
        return
    if kind == "adventures":
        count = _count(db, models.Adventure, models.Adventure.user_id == user.id)
        cap, subject, hint = (
            MAX_ADVENTURES_PER_USER, "adventures",
            "delete one you no longer play to make room",
        )
    elif kind == "scenarios":
        count = _count(db, models.Scenario, models.Scenario.user_id == user.id)
        cap, subject, hint = (
            MAX_SCENARIOS_PER_USER, "scenarios", "delete one to make room"
        )
    elif kind == "scripts":
        count = _count(db, models.Script, models.Script.user_id == user.id)
        cap, subject, hint = (
            MAX_SCRIPTS_PER_USER, "scripts", "delete one to make room"
        )
    elif kind == "story_cards":
        owner_filter = (
            models.StoryCard.scenario_id == scenario_id
            if scenario_id is not None
            else models.StoryCard.adventure_id == adventure_id
        )
        count = _count(db, models.StoryCard, owner_filter)
        cap, subject, hint = (
            MAX_STORY_CARDS_PER_OWNER, "story cards here", "delete one to make room"
        )
    elif kind == "memories":
        count = _count(db, models.Memory, models.Memory.adventure_id == adventure.id)
        cap, subject, hint = (
            MAX_MEMORIES_PER_ADVENTURE, "memories in this adventure",
            "delete some to make room",
        )
    elif kind == "actions":
        # Count every action in the adventure, which is the whole tree rather
        # than the path being played. That number is what costs storage, and
        # nothing is pruned automatically, so it is the right one to cap. It does
        # mean a heavily branched adventure reaches the cap while its story is
        # shorter than the cap, which is why the message counts "actions in this
        # adventure" rather than turns.
        count = _count(db, models.Action, models.Action.adventure_id == adventure.id)
        cap, subject, hint = (
            MAX_ACTIONS_PER_ADVENTURE, "actions in this adventure",
            "export it and continue in a new adventure",
        )
    else:  # pragma: no cover. This is a programming error, not user input.
        raise ValueError(f"Unknown row cap kind: {kind}")
    if count >= cap:
        raise HTTPException(409, f"You've reached the limit of {cap} {subject} — {hint}.")


def _count(db: Session, model, condition) -> int:
    return db.query(func.count(model.id)).filter(condition).scalar() or 0


_BUNDLE_LIST_CAPS = {
    "story_cards": MAX_STORY_CARDS_PER_OWNER,
    "memories": MAX_MEMORIES_PER_ADVENTURE,
    "actions": MAX_ACTIONS_PER_ADVENTURE,
    "branches": MAX_BRANCHES_PER_ADVENTURE,
}


def check_bundle_lists(**lists) -> None:
    """Raises a 409 when an import bundle's lists exceed the caps live creation uses.

    The keyword arguments are `story_cards`, `memories`, `actions`, and
    `branches`.
    """
    if not auth.MULTI_USER:
        return
    for name, value in lists.items():
        cap = _BUNDLE_LIST_CAPS[name]
        if isinstance(value, list) and len(value) > cap:
            noun = name.replace("_", " ")
            raise HTTPException(
                409, f"This file contains {len(value)} {noun} — the limit is {cap}."
            )


# ---------- Request body size ----------
# The limit is generous enough for the largest legitimate payload, which is an
# adventure export holding thousands of actions. It applies in every mode, and no
# honest request approaches it.

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_IMPORT_BODY_BYTES = 20 * 1024 * 1024


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies by their declared `Content-Length`.

    This is pure ASGI rather than `BaseHTTPMiddleware`, so SSE responses stream
    through unchanged. A chunked upload with no length is refused, because every
    real client of this API sends `Content-Length`, including browser fetch and
    curl with a file.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") in ("POST", "PUT", "PATCH"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            limit = (
                MAX_IMPORT_BODY_BYTES
                if scope.get("path", "").endswith("/import")
                else MAX_BODY_BYTES
            )
            length = headers.get("content-length")
            problem = None
            if length is None:
                if "chunked" in headers.get("transfer-encoding", "").lower():
                    problem = (411, "Content-Length is required.")
            else:
                try:
                    if int(length) > limit:
                        problem = (
                            413,
                            f"Request too large (limit {limit // (1024 * 1024)} MB).",
                        )
                except ValueError:
                    problem = (400, "Invalid Content-Length.")
            if problem:
                await _send_json_error(send, *problem)
                return
        await self.app(scope, receive, send)


async def _send_json_error(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
