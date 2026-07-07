import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import auth, limits, models, schemas, security
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
    """Who am I? In multi-user mode this also bootstraps the session: with no
    (or an invalid) cookie it creates a guest user and sets one — the
    frontend calls this on load and after any 401."""
    if not auth.MULTI_USER:
        user = auth.local_user(db)
    else:
        user = auth.resolve_session_user(request, db)
        if user is None:
            # Each new guest is a database row — cap how fast one IP can mint them.
            limits.rate_limit("guest", request)
            user = models.User(is_guest=True)
            db.add(user)
            db.commit()
            _set_session_cookie(response, user.id)
    return me_payload(user, db)


@router.post("/register")
def register(
    payload: schemas.AuthCredentials,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Upgrade the current guest in place — same user_id, so every adventure,
    scenario, script and setting they created as a guest is kept."""
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
    user = db.query(models.User).filter(models.User.email == email).first()
    if (
        user is None
        or not user.password_hash
        or not security.verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(401, "Incorrect email or password.")
    _set_session_cookie(response, user.id)
    return me_payload(user, db)


@router.post("/logout")
def logout(response: Response):
    if not auth.MULTI_USER:
        raise HTTPException(400, "Accounts are disabled in local mode.")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}
