"""Phase 9 — abuse guards for hosted (multi-user) deployments.

Rate limits and row caps are no-ops in local mode: a single local player
should never be throttled by their own app. Values are hardcoded on purpose —
generous enough that a legitimate player never notices, tight enough that a
hostile visitor can't burn the demo key, peg the CPU, or bloat the database.
"""

import json
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import auth, models

# ---------- Rate limiting ----------
# Fixed windows per (scope, caller). In-memory: fine for the single-process
# deployment this app targets (and the worst case after a restart is a brief
# extra allowance).

# scope -> (max requests, window seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "turn": (10, 60),             # AI turn generation (demo key also has a daily cap)
    "script-test": (30, 60),      # sandboxed, but each run costs up to 2s CPU
    "connection-test": (10, 60),  # outbound HTTP to a user-supplied URL
    "import": (30, 60),           # large writes
    "auth": (10, 300),            # register/login attempts, per IP
    "guest": (30, 300),           # new guest users, per IP (each is a DB row)
}

_windows: dict[tuple[str, str], deque] = defaultdict(deque)
_windows_guard = threading.Lock()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(scope: str, request: Request, user: models.User | None = None) -> None:
    """429 when the caller exceeds the scope's window. Keyed per user when one
    is known (accounts survive IP changes), per IP otherwise."""
    if not auth.MULTI_USER:
        return
    limit, window_seconds = RATE_LIMITS[scope]
    key = (scope, f"u{user.id}" if user else f"ip{_client_ip(request)}")
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


def _prune(now: float) -> None:
    """Drop callers whose whole window has expired (call with guard held) so
    the per-IP dict can't grow without bound."""
    longest = max(seconds for _, seconds in RATE_LIMITS.values())
    stale = [key for key, window in _windows.items()
             if not window or window[-1] < now - longest]
    for key in stale:
        del _windows[key]


# ---------- Per-user row caps ----------

MAX_ADVENTURES_PER_USER = 100
MAX_SCENARIOS_PER_USER = 200
MAX_SCRIPTS_PER_USER = 200
MAX_STORY_CARDS_PER_OWNER = 200   # per scenario or adventure
MAX_MEMORIES_PER_ADVENTURE = 1000
MAX_ACTIONS_PER_ADVENTURE = 5000


def check_row_cap(
    kind: str,
    db: Session,
    user: models.User,
    *,
    adventure: models.Adventure | None = None,
    scenario_id: int | None = None,
    adventure_id: int | None = None,
) -> None:
    """409 with a friendly message when creating one more row of `kind` would
    exceed its cap. Ownership of the passed scenario/adventure has already
    been checked by the caller."""
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
        count = _count(db, models.Action, models.Action.adventure_id == adventure.id)
        cap, subject, hint = (
            MAX_ACTIONS_PER_ADVENTURE, "actions in this adventure",
            "export it and continue in a new adventure",
        )
    else:  # pragma: no cover — programming error, not user input
        raise ValueError(f"Unknown row cap kind: {kind}")
    if count >= cap:
        raise HTTPException(409, f"You've reached the limit of {cap} {subject} — {hint}.")


def _count(db: Session, model, condition) -> int:
    return db.query(func.count(model.id)).filter(condition).scalar() or 0


_BUNDLE_LIST_CAPS = {
    "story_cards": MAX_STORY_CARDS_PER_OWNER,
    "memories": MAX_MEMORIES_PER_ADVENTURE,
    "actions": MAX_ACTIONS_PER_ADVENTURE,
}


def check_bundle_lists(**lists) -> None:
    """409 when an import bundle's lists exceed the same caps live creation
    enforces (kwargs: story_cards=, memories=, actions=)."""
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
# Generous enough for the biggest legitimate payload (an adventure export with
# thousands of actions), applied in every mode — no honest request comes close.

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_IMPORT_BODY_BYTES = 20 * 1024 * 1024


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies by declared Content-Length. Pure ASGI
    (not BaseHTTPMiddleware) so SSE responses stream through untouched.
    Chunked uploads without a length are refused — every real client of this
    API (browser fetch, curl with a file) sends Content-Length."""

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
