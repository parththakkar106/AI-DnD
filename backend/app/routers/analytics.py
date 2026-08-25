"""Visit analytics: one endpoint the browser writes to, one the owner reads.

The split matters. `/collect` is public and accepts one fact, which page was
viewed, because anything a stranger can POST is a number a stranger can invent.
Everything the dashboard relies on, meaning turns, adventures, sign-ups, demo
spend, and errors, is recorded on the server by the code that performs it, so
those counts are as trustworthy as the app itself.

The two reading endpoints are owner-only and 404 for everyone else, the same
way the AI Chat router does: a feature nobody else can use is better off not
appearing to exist. `/summary` serves the anonymous counters (analytics.py,
which stores nothing that points at a person) and `/access` serves the access
log (accesslog.py, which identifies people on purpose).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import accesslog, analytics, auth, limits, models
from ..database import get_db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def owner(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
) -> models.User:
    """Gates the reading half. It returns 404 rather than 403. See the module
    docstring."""
    if not auth.is_owner(user):
        raise HTTPException(404, "Not found")
    return user


Owner = Depends(owner)


class Pageview(BaseModel):
    """What the SPA reports on a page load or a route change.

    `first` marks a real page load rather than a client-side navigation. The
    facts that describe a visit rather than a view, which are where it came from,
    on what kind of device, and from which country, are recorded only on a page
    load, so a visitor who clicks through five pages is still one referral.
    """

    path: str = Field("", max_length=300)
    referrer: str = Field("", max_length=500)
    first: bool = False


@router.post("/collect", status_code=204)
def collect(
    payload: Pageview,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Record one pageview. Always 204, even when nothing was counted: the
    browser has no business knowing whether it was."""
    limits.rate_limit("analytics", request)
    # Resolved by hand rather than through get_current_user: a pageview that
    # arrives before /auth/me has minted a session should still be counted as a
    # view, not turned into a 401 the SPA has to handle.
    user = (
        auth.resolve_session_user(request, db)
        if auth.MULTI_USER
        else auth.local_user(db)
    )
    # The operator's own clicks are not traffic. This applies only in
    # multi-user mode. Locally every user is the owner, and excluding them would
    # leave the dashboard empty on the machine the app is developed on.
    if auth.MULTI_USER and user is not None and auth.is_owner(user):
        return Response(status_code=204)

    analytics.record(analytics.M_PAGE, analytics.normalize_route(payload.path))
    analytics.record_visit(user)
    if payload.first:
        referrer = analytics.normalize_referrer(
            payload.referrer, request.url.hostname or ""
        )
        if referrer:  # "" means same-origin, which is not a referral
            analytics.record(analytics.M_REFERRER, referrer)
        analytics.record(
            analytics.M_DEVICE,
            analytics.device_of(request.headers.get("user-agent", "")),
        )
        analytics.record(analytics.M_COUNTRY, analytics.country_of(request.headers))
    return Response(status_code=204)


@router.get("/summary")
def summary(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    _user: models.User = Owner,
) -> dict:
    """Returns the whole dashboard in one aggregate response.

    The response is a few kilobytes however much traffic is behind it.
    """
    return analytics.summary(db, days)


@router.get("/access")
def access_log(
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None),
    kind: str | None = Query(None),
    q: str | None = Query(None, max_length=120),
    db: Session = Depends(get_db),
    _user: models.User = Owner,
) -> dict:
    """A page of the access log, newest first.

    Unlike `/summary`, this returns rows about people, which is what it is for.
    It is therefore behind the same owner gate, it is paged rather than returned
    in full, and the people it describes have no endpoint that reaches it.
    """
    page = accesslog.recent(
        db, limit=limit, before_id=before_id, kind=kind, query=q
    )
    return {
        "events": [
            {
                "id": event.id,
                "at": event.at.isoformat(),
                "kind": event.kind,
                "who": event.who,
                "is_guest": event.is_guest,
                "ip": event.ip,
                "country": event.country,
                "device": event.device,
                "user_agent": event.user_agent,
            }
            for event in page["events"]
        ],
        "has_more": page["has_more"],
    }
