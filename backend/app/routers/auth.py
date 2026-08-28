import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import (accesslog, analytics, auth, cleanup, limits, models, schemas,
                security, starter)
from ..database import get_db
from .settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _set_session_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        security.sign_session(user_id),
        max_age=auth.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=auth.COOKIE_SECURE,
        path="/",
    )


def me_payload(user: models.User, db: Session) -> dict:
    settings = get_settings(db, user)
    cfg = auth.resolve_provider_config(settings)
    return {
        "multi_user": auth.MULTI_USER,
        "id": user.id,
        "email": user.email,
        "is_guest": user.is_guest,
        # Trusted testers: unmetered demo turns, plus the AI Chat scratchpad.
        "power_user": auth.is_power_user(user),
        # Separate allowlist: shows the visit-analytics page and its nav link.
        "analytics": auth.is_owner(user),
        # How long an idle guest is kept before cleanup deletes it (None when
        # the policy is off). Served rather than hardcoded in the UI so the
        # number a guest is shown is the number actually enforced.
        "guest_retention_days": cleanup.RETENTION_DAYS if cleanup.enabled() else None,
        "demo": {
            "enabled": auth.demo_enabled(),
            "using_demo": cfg.using_demo,
            "model": cfg.model if cfg.using_demo else None,
            "turns_per_day": auth.DEMO_TURNS_PER_DAY,
            "turns_left": auth.demo_turns_left(user) if auth.demo_enabled() else None,
            "models": auth.DEMO_MODELS if auth.demo_enabled() else [],
        },
    }


@router.get("/me")
def me(request: Request, response: Response, db: Session = Depends(get_db)):
    """Returns the current user.

    In multi-user mode this also establishes the session. If the cookie is
    missing or invalid, the endpoint creates a guest user and sets a cookie. The
    frontend calls it on load and after any 401.
    """
    if not auth.MULTI_USER:
        user = auth.local_user(db)
    else:
        user = auth.resolve_session_user(request, db)
        if user is None:
            # Each new guest is a database row, so cap how fast one IP can
            # create them.
            limits.rate_limit("guest", request)
            user = models.User(is_guest=True)
            db.add(user)
            db.commit()
            # The guest is committed first, so a failure while copying the
            # starter adventure still leaves them with an account.
            starter.give(db, user)
            db.commit()
            _set_session_cookie(response, user.id)
    # This endpoint is the SPA's bootstrap call, so it is where a session first
    # shows itself; accesslog thins the rows down to one per day per address.
    accesslog.note_session(db, user, request)
    return me_payload(user, db)


@router.post("/register")
def register(
    payload: schemas.AuthCredentials,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Upgrades the current guest in place.

    The `user_id` does not change, so every adventure, scenario, script, and
    setting they created as a guest is kept.
    """
    if not auth.MULTI_USER:
        raise HTTPException(400, "Accounts are disabled in local mode.")
    limits.rate_limit("auth", request)
    email = payload.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    if len(payload.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters.")
    if not user.is_guest:
        raise HTTPException(400, "This session is already registered.")
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(409, "An account with this email already exists — log in instead.")
    user.email = email
    user.password_hash = security.hash_password(payload.password)
    user.is_guest = False
    db.commit()
    analytics.record_event(analytics.EV_SIGNUP, user)
    accesslog.record(db, accesslog.REGISTER, request, user=user)
    return me_payload(user, db)


@router.post("/login")
def login(
    payload: schemas.AuthCredentials,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Point this browser's session at an existing account. Any current guest
    session is simply abandoned (its data stays under the guest user)."""
    if not auth.MULTI_USER:
        raise HTTPException(400, "Accounts are disabled in local mode.")
    limits.rate_limit("auth", request)
    email = payload.email.strip().lower()
    # Per-account throttle: stops distributed guessing against one email even
    # when the per-IP limit above is diluted across many source addresses.
    limits.check_login_allowed(email)
    user = db.query(models.User).filter(models.User.email == email).first()
    if (
        user is None
        or not user.password_hash
        or not security.verify_password(payload.password, user.password_hash)
    ):
        limits.note_login_failure(email)
        # Logged with the address that was tried, not the account that owns it:
        # a guessing run against an address that has no account is exactly the
        # thing worth being able to see.
        accesslog.record(db, accesslog.LOGIN_FAILED, request, who=email)
        raise HTTPException(401, "Incorrect email or password.")
    limits.note_login_success(email)
    _set_session_cookie(response, user.id)
    analytics.record_event(analytics.EV_LOGIN, user)
    accesslog.record(db, accesslog.LOGIN, request, user=user)
    return me_payload(user, db)


@router.post("/logout")
def logout(response: Response):
    if not auth.MULTI_USER:
        raise HTTPException(400, "Accounts are disabled in local mode.")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}
