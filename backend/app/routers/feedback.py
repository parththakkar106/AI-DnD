"""Player feedback: anyone with a session can send it, and only the owner reads it.

The send endpoint accepts a message and an optional reply address. Each account
can send `DAILY_LIMIT` messages per UTC day. The page does not state the limit,
because it exists only to slow spam. The list endpoint reuses the owner gate
from the analytics router, so it returns 404 for everyone else.
"""

import re
from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import accesslog, auth, limits, models
from ..database import get_db
from .analytics import Owner

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

DAILY_LIMIT = 5
# A loose shape check. The owner replies by hand, so a typo costs one bounced
# email, and a strict pattern would reject real addresses.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class FeedbackIn(BaseModel):
    message: str = Field(..., max_length=5000)
    email: str = Field("", max_length=320)


@router.post("", status_code=201)
def send_feedback(
    payload: FeedbackIn,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
) -> dict:
    message = payload.message.strip()
    email = payload.email.strip()
    if not message:
        raise HTTPException(422, "Write a message before sending.")
    if email and not _EMAIL.match(email):
        raise HTTPException(422, "That email address doesn't look right.")
    # Local mode has one player, who is also the owner, so it skips the cap like
    # every other limit in `limits`.
    if auth.MULTI_USER:
        today = datetime.combine(datetime.now(timezone.utc).date(), time.min)
        sent = (
            db.query(func.count(models.Feedback.id))
            .filter(models.Feedback.user_id == user.id, models.Feedback.at >= today)
            .scalar()
        )
        if sent >= DAILY_LIMIT:
            raise HTTPException(
                429, "You've sent a lot of feedback today. Try again tomorrow."
            )
    db.add(models.Feedback(
        user_id=user.id,
        who=accesslog.describe(user),
        is_guest=user.is_guest,
        contact_email=email,
        message=message,
        ip=limits.client_ip(request),
    ))
    db.commit()
    return {"ok": True}


@router.get("")
def list_feedback(
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None),
    db: Session = Depends(get_db),
    _user: models.User = Owner,
) -> dict:
    """Returns a page of feedback, newest first."""
    query = db.query(models.Feedback).order_by(models.Feedback.id.desc())
    if before_id is not None:
        query = query.filter(models.Feedback.id < before_id)
    rows = query.limit(limit + 1).all()
    return {
        "items": [
            {
                "id": row.id,
                "at": row.at.isoformat(),
                "user_id": row.user_id,
                "who": row.who,
                "is_guest": row.is_guest,
                "email": row.contact_email,
                "message": row.message,
                "ip": row.ip,
            }
            for row in rows[:limit]
        ],
        "has_more": len(rows) > limit,
    }
