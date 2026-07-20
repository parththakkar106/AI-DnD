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
    BYOK-vs-demo decision."""

    endpoint_url: str
    api_key: str
    model: str
    using_demo: bool


def resolve_provider_config(settings: models.Settings) -> ProviderConfig:
    key = settings.api_key_plain
    if key or not demo_enabled():
        return ProviderConfig(settings.endpoint_url, key, settings.model, False)
    model = settings.model if settings.model in DEMO_MODELS else DEMO_MODELS[0]
    return ProviderConfig(DEMO_ENDPOINT_URL, DEMO_API_KEY, model, True)


def _today() -> str:
    return models.utcnow().date().isoformat()


def is_power_user(user: models.User) -> bool:
    """Trusted testers (email allowlist) bypass the demo turn cap."""
    return bool(user.email) and user.email.lower() in POWER_USERS


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
