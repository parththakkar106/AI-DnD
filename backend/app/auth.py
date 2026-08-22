"""Phase 8 — user resolution, sessions, and the shared demo key.

Two modes, chosen by the AIDND_MULTI_USER env var:

- Local mode (default): every request resolves to one auto-created "local
  user". No cookies, no login UI — a clone/docker-compose behaves exactly
  like the pre-Phase-8 single-user app.
- Multi-user mode (hosted): requests carry a signed session cookie. GET
  /api/auth/me creates a guest user on first visit; registering upgrades the
  guest in place so their data survives. Requests without a valid session get
  401 and the frontend re-establishes via /me.

The shared demo key (BYOK fallback) is also configured here: users whose
settings have no API key are routed to a server-funded endpoint with a model
whitelist and a per-day turn cap.
"""

import os
from dataclasses import dataclass
from datetime import timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import models, security
from .database import get_db


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


MULTI_USER = _env_flag("AIDND_MULTI_USER")

SESSION_COOKIE = "aidnd_session"
# Secure cookies default on in multi-user (hosted = HTTPS; browsers also
# accept Secure on http://localhost). AIDND_COOKIE_SECURE=0/1 overrides —
# e.g. 0 when testing multi-user over plain http on a LAN address.
_cookie_secure_env = os.environ.get("AIDND_COOKIE_SECURE", "").strip().lower()
COOKIE_SECURE = (
    _cookie_secure_env in ("1", "true", "yes", "on")
    if _cookie_secure_env
    else MULTI_USER
)
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# ---------- Shared demo key (BYOK fallback) ----------

DEMO_API_KEY = os.environ.get("AIDND_DEMO_API_KEY", "").strip()
DEMO_ENDPOINT_URL = (
    os.environ.get("AIDND_DEMO_ENDPOINT_URL", "").strip()
    or "https://openrouter.ai/api/v1"
)
DEMO_MODELS = [
    m.strip()
    for m in os.environ.get("AIDND_DEMO_MODELS", "").split(",")
    if m.strip()
] or ["google/gemma-4-26b-a4b-it:free"]
DEMO_TURNS_PER_DAY = int(os.environ.get("AIDND_DEMO_TURNS_PER_DAY", "20") or 20)

# Trusted testers (by email) who bypass the daily demo cap — unmetered turns on
# the shared demo key. Comma-separated emails; matched case-insensitively.
POWER_USERS = {
    e.strip().lower()
    for e in os.environ.get("AIDND_POWER_USERS", "").split(",")
    if e.strip()
}

# Who can see the visit analytics. Deliberately its own list rather than
# POWER_USERS: a trusted tester gets unmetered turns and the AI Chat page,
# which is not a reason to hand them the site's traffic numbers. Empty (the
# default) means nobody sees the dashboard in a hosted deployment.
ANALYTICS_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("AIDND_ANALYTICS_EMAILS", "").split(",")
    if e.strip()
}

DEMO_CAP_MESSAGE = (
    f"You've used all {DEMO_TURNS_PER_DAY} free demo turns for today. "
    "Add your own API key in Settings to keep playing (it resets tomorrow)."
)


def demo_enabled() -> bool:
    # The demo key is a hosted-deployment feature; local installs talk to
    # whatever endpoint Settings points at, even with no API key (Ollama).
    return MULTI_USER and bool(DEMO_API_KEY)


@dataclass
class ProviderConfig:
    """What the turn engine should actually connect with, after the
    BYOK-vs-demo decision. Build these with resolve_provider_config()."""

    endpoint_url: str
    api_key: str
    model: str
    using_demo: bool

    def __post_init__(self) -> None:
        # Belt and braces around server-funded turns: resolve_provider_config()
        # already pins the model, and this makes it a property of the config
        # object too, so a future caller can't construct an unpinned one.
        # Unreachable by design — a raise here means a new code path bypassed
        # the pinning, which is worth failing loudly rather than billing.
        #
        # The test is `using_demo`, NOT `api_key == DEMO_API_KEY`. Keying it on
        # the key value looks stricter but is wrong: the demo key is a normal
        # OpenRouter key, so a user can legitimately paste that same key into
        # their own Settings as BYOK — and then every resolution raised, 500ing
        # even GET /auth/me and taking the whole SPA down with it. `using_demo`
        # is what actually means "the server is paying", and only the demo
        # branch below sets it.
        if self.using_demo and self.model not in DEMO_MODELS:
            raise ValueError(
                f"Refusing to use the shared demo key with non-whitelisted model {self.model!r}"
            )


def resolve_provider_config(
    settings: models.Settings, *, model_override: str | None = None
) -> ProviderConfig:
    """BYOK when the user has their own key, the shared demo key otherwise.

    THE security-relevant branch is the demo one, and it is the only place the
    whitelist rule lives — every caller must come through here rather than
    building a ProviderConfig itself. On the demo key:

    - the model is pinned to DEMO_MODELS, so a caller-supplied override (the AI
      Chat page) or a hand-edited Settings row cannot aim a server-funded key at
      a paid model; anything unrecognised falls back to DEMO_MODELS[0];
    - the endpoint is pinned to DEMO_ENDPOINT_URL, so the key itself can't be
      redirected to a URL the user controls and harvested.

    `model_override` is a per-request preference (never a grant): it's honoured
    verbatim under BYOK, and only if whitelisted on the demo key.
    """
    key = settings.api_key_plain
    requested = (model_override or "").strip() or settings.model
    if key or not demo_enabled():
        return ProviderConfig(settings.endpoint_url, key, requested, False)
    model = requested if requested in DEMO_MODELS else DEMO_MODELS[0]
    return ProviderConfig(DEMO_ENDPOINT_URL, DEMO_API_KEY, model, True)


def _today() -> str:
    return models.utcnow().date().isoformat()


def is_power_user(user: models.User) -> bool:
    """Trusted testers: unmetered demo turns, plus tooling that isn't part of
    the game (the AI Chat scratchpad). Local installs are always trusted — it's
    the operator's own machine and their own API key, same reasoning as the
    provider debug log being local-only."""
    if not MULTI_USER:
        return True
    return bool(user.email) and user.email.lower() in POWER_USERS


def is_owner(user: models.User) -> bool:
    """May this user see the visit analytics? Local installs always can — it is
    the operator's own machine and their own visits, same reasoning as the
    provider debug log; hosted deployments check AIDND_ANALYTICS_EMAILS."""
    if not MULTI_USER:
        return True
    return bool(user.email) and user.email.lower() in ANALYTICS_EMAILS


def demo_turns_left(user: models.User) -> int:
    # Power users are never capped; report the full cap so the banner reads
    # "N of N" rather than a decrementing count.
    if is_power_user(user):
        return DEMO_TURNS_PER_DAY
    used = user.demo_turns_used if user.demo_turns_date == _today() else 0
    return max(0, DEMO_TURNS_PER_DAY - used)


def count_demo_turn(user: models.User) -> None:
    """Record one demo turn; the caller's commit persists it."""
    if is_power_user(user):
        return  # unmetered — power users don't count against the cap
    today = _today()
    if user.demo_turns_date != today:
        user.demo_turns_date = today
        user.demo_turns_used = 0
    user.demo_turns_used += 1


# ---------- User resolution ----------

def local_user(db: Session) -> models.User:
    """The single implicit user in local mode (owns pre-Phase-8 data via
    migration; created lazily on a fresh database)."""
    user = (
        db.query(models.User)
        .filter(models.User.email.is_(None), models.User.is_guest.is_(False))
        .order_by(models.User.id)
        .first()
    )
    if user is None:
        user = models.User(is_guest=False)
        db.add(user)
        db.commit()
    return user


def _touch(user: models.User, db: Session) -> None:
    now = models.utcnow()
    last = user.last_seen_at
    if last is not None and last.tzinfo is None:
        # SQLite hands DateTime columns back naive; they were stored as UTC.
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (now - last).total_seconds() > 3600:
        user.last_seen_at = now
        db.commit()


def resolve_session_user(request: Request, db: Session) -> models.User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = security.verify_session(token)
    if user_id is None:
        return None
    return db.get(models.User, user_id)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Dependency used by every router. 401 in multi-user mode means the
    frontend must (re)establish a session via GET /api/auth/me."""
    if not MULTI_USER:
        user = local_user(db)
    else:
        user = resolve_session_user(request, db)
        if user is None:
            raise HTTPException(401, "No session. Call GET /api/auth/me first.")
    _touch(user, db)
    return user
