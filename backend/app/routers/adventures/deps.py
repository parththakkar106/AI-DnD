"""The router every endpoint module registers on, and the dependencies they share.

This module imports nothing else in the package. Keeping it at the bottom of the
import graph is what lets each endpoint module import the router without
importing its siblings.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import auth, models
from ...database import get_db


router = APIRouter(prefix="/api/adventures", tags=["adventures"])

CurrentUser = Depends(auth.get_current_user)


def get_adventure_or_404(
    adventure_id: int, db: Session, user: models.User
) -> models.Adventure:
    adventure = db.get(models.Adventure, adventure_id)
    if adventure is None or adventure.user_id != user.id:
        raise HTTPException(404, "Adventure not found")
    return adventure


def current_adventure(
    adventure_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
) -> models.Adventure:
    """Resolves the `{adventure_id}` in the path to the caller's own adventure.

    Declare this in a handler's signature. The ownership check then reads in the
    signature, where you look for it, and it runs before the handler body.

    FastAPI caches a dependency for the length of one request, so the handler's
    own `db` is the same session this one loaded the adventure from.
    """
    return get_adventure_or_404(adventure_id, db, user)
