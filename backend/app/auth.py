"""Phase 8: user resolution, sessions, and the shared demo key.

The `AIDND_MULTI_USER` environment variable selects one of two modes:

* Local mode, the default. Every request resolves to one automatically created
  local user. There are no cookies and no login UI, so a clone or a
  docker-compose run behaves like the single-user app from before Phase 8.
* Multi-user mode, used for hosted deployments. Requests carry a signed session
  cookie. `GET /api/auth/me` creates a guest user on the first visit, and
  registering upgrades that guest in place so their data survives. A request
  without a valid session gets a 401, and the frontend re-establishes the
  session through `/me`.

The shared demo key, which is the fallback when a user brings no key of their
own, is also configured here. A user whose settings hold no API key is routed to
a server-funded endpoint with a model allowlist and a per-day turn cap.
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
# Secure cookies are on by default in multi-user mode, because a hosted
# deployment serves HTTPS and browsers also accept Secure on http://localhost.
# `AIDND_COOKIE_SECURE` overrides the default with 0 or 1. Use 0 when testing
# multi-user mode over plain HTTP on a LAN address.
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

# Trusted testers, listed by email, who bypass the daily demo cap and take
# unmetered turns on the shared demo key. The list is comma-separated, and the
# match ignores case.
POWER_USERS = {
    e.strip().lower()
    for e in os.environ.get("AIDND_POWER_USERS", "").split(",")
    if e.strip()
}

# Who can see the visit analytics. This is a separate list from `POWER_USERS` on
# purpose. A trusted tester gets unmetered turns and the AI Chat page, which is
# not a reason to give them the site's traffic numbers. An empty list, which is
# the default, means nobody sees the dashboard in a hosted deployment.
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
    # The demo key is a hosted-deployment feature. A local install talks to
    # whatever endpoint Settings points at, even with no API key, such as
    # Ollama.
    return MULTI_USER and bool(DEMO_API_KEY)


@dataclass
class ProviderConfig:
    """What the turn engine connects with, after the decision between a
    user-supplied key and the demo key.

    Build one of these with `resolve_provider_config()`.
    """

    endpoint_url: str
    api_key: str
    model: str
    using_demo: bool

    def __post_init__(self) -> None:
        # A second guard around server-funded turns. `resolve_provider_config()`
        # already pins the model, and this makes the pin a property of the config
        # object too, so a later caller cannot construct an unpinned one. This
        # raise is unreachable by design. Reaching it means a new code path
        # bypassed the pinning, which is worth failing on rather than billing
        # for.
        #
        # The test is `using_demo`, not `api_key == DEMO_API_KEY`. Keying on the
        # key value looks stricter and is wrong. The demo key is an ordinary
        # OpenRouter key, so a user can legitimately paste that same key into
        # their own Settings. Every resolution then raised, which returned a 500
        # even from `GET /auth/me` and took the whole SPA down. `using_demo` is
        # what means the server is paying, and only the demo branch below sets
        # it.
        if self.using_demo and self.model not in DEMO_MODELS:
            raise ValueError(
                f"Refusing to use the shared demo key with non-whitelisted model {self.model!r}"
            )


def resolve_provider_config(
    settings: models.Settings, *, model_override: str | None = None
) -> ProviderConfig:
    """Returns the user's own key when they have one, and the shared demo key
    otherwise.

    The demo branch is the security-relevant one, and it is the only place the
    allowlist rule lives. Every caller has to come through this function rather
    than build a `ProviderConfig` itself. On the demo key:

    * The model is pinned to `DEMO_MODELS`, so a caller-supplied override from
      the AI Chat page, or a hand-edited Settings row, cannot point a
      server-funded key at a paid model. An unrecognized model falls back to
      `DEMO_MODELS[0]`.
    * The endpoint is pinned to `DEMO_ENDPOINT_URL`, so the key cannot be
      redirected to a URL the user controls and captured there.

    `model_override` is a per-request preference and never a grant. It is used
    verbatim with the user's own key, and on the demo key only when the model is
    on the allowlist.
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
    """Returns whether this user is a trusted tester.

    A trusted tester gets unmetered demo turns, plus tooling that is not part of
    the game, such as the AI Chat scratchpad. A local install is always trusted,
    because it runs on the operator's own machine with their own API key. The
    provider debug log is local-only for the same reason.
    """
    if not MULTI_USER:
        return True
    return bool(user.email) and user.email.lower() in POWER_USERS


def is_owner(user: models.User) -> bool:
    """Returns whether this user may see the visit analytics.

    A local install always may, because it runs on the operator's own machine and
    shows their own visits. The provider debug log follows the same reasoning. A
    hosted deployment checks `AIDND_ANALYTICS_EMAILS`.
    """
    if not MULTI_USER:
        return True
    return bool(user.email) and user.email.lower() in ANALYTICS_EMAILS


def demo_turns_left(user: models.User) -> int:
    # A power user is never capped, so report the full cap and let the banner
    # read "N of N" rather than count down.
    if is_power_user(user):
        return DEMO_TURNS_PER_DAY
    used = user.demo_turns_used if user.demo_turns_date == _today() else 0
    return max(0, DEMO_TURNS_PER_DAY - used)


def count_demo_turn(user: models.User) -> None:
    """Records one demo turn. The caller's commit stores it."""
    if is_power_user(user):
        return  # A power user's turns do not count against the cap.
    today = _today()
    if user.demo_turns_date != today:
        user.demo_turns_date = today
        user.demo_turns_used = 0
    user.demo_turns_used += 1


# ---------- User resolution ----------

def local_user(db: Session) -> models.User:
    """Returns the single implicit user used in local mode.

    A migration gives this user ownership of data written before Phase 8. On a
    fresh database the user is created on first use.
    """
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
        # SQLite returns DateTime columns without a timezone. They were stored
        # as UTC.
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
    """The dependency every router uses to resolve the current user.

    In multi-user mode a 401 means the frontend has to establish a session again
    through `GET /api/auth/me`.
    """
    if not MULTI_USER:
        user = local_user(db)
    else:
        user = resolve_session_user(request, db)
        if user is None:
            raise HTTPException(401, "No session. Call GET /api/auth/me first.")
    _touch(user, db)
    return user
