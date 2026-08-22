"""The access log: who arrived, when, and from where.

The deliberate opposite of analytics.py. That module counts and stores nothing
that points at a person; this one records addresses, email addresses and
devices, because an access log that cannot identify the access is not an access
log. They are kept in separate modules and separate tables on purpose — the
anonymity of the counters is then a property of the code rather than of a
convention someone has to remember.

Owner-only, and never shown to the people it records.

Four kinds of row:

- `session`      a browser that has a session made a request — for a guest,
                 their first visit;
- `login`        an existing account signed in;
- `register`     a guest upgraded to an account;
- `login_failed` a password attempt that didn't match, with the address tried.

Session rows are the only ones that need thinning: `/auth/me` runs on every
page load, and a row per load would be noise rather than a log. One is written
when the day or the address changes for that user, which is the granularity a
log is actually read at — "seen on the 3rd from 1.2.3.4" — and it still catches
someone moving networks mid-day.
"""

import logging
import threading

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from . import analytics, models

logger = logging.getLogger(__name__)

SESSION = "session"
LOGIN = "login"
REGISTER = "register"
LOGIN_FAILED = "login_failed"

MAX_UA = 200

# user id -> (day, ip) of the last session row written for them. Process-local
# like the rate limiter's windows, and for the same reason: this is a single
# process, and the worst case after a restart is one redundant row per user.
_last_session: dict[int, tuple[str, str]] = {}
_guard = threading.Lock()
_MAX_TRACKED = 10_000


def _client_ip(request) -> str:
    # Deferred: limits imports auth, which is imported by the routers that call
    # this, so a module-level import here would close the loop. The spoof
    # resistance lives there and must not be reimplemented — a second, laxer
    # copy of "what is the client's address" is exactly how one of them ends up
    # trusting a header it shouldn't.
    from . import limits

    return limits.client_ip(request)


def describe(user: models.User) -> str:
    """How a user is named in the log. Guests have no email, and their id is
    the only handle anyone has for them. The third case is a local install's
    implicit single user, which is also email-less but is the operator rather
    than a visitor — calling that one "Guest #1" would be a small lie in the
    one row they are certain to read."""
    if user.email:
        return user.email
    return f"Guest #{user.id}" if user.is_guest else f"Local user #{user.id}"


def _country(request) -> str:
    """The edge's country header, or "" when there isn't one. Blank rather than
    the counters' "(unknown)" label: a table column reads better as an em dash
    than as a word, and an empty string is the honest value for "not known"."""
    country = analytics.country_of(request.headers)
    return "" if country == analytics.UNKNOWN else country


def record(
    db: Session,
    kind: str,
    request,
    *,
    user: models.User | None = None,
    who: str | None = None,
) -> None:
    """Write one row. Never raises: the log watches sign-in, it doesn't guard
    it, and a logging failure must not be able to lock anyone out."""
    try:
        event = models.AccessEvent(
            kind=kind,
            user_id=user.id if user is not None else None,
            who=(who if who is not None else describe(user) if user else "")[:320],
            is_guest=bool(user.is_guest) if user is not None else False,
            ip=_client_ip(request)[:45],
            country=_country(request),
            device=analytics.device_of(request.headers.get("user-agent", "")),
            user_agent=(request.headers.get("user-agent") or "")[:MAX_UA],
        )
        db.add(event)
        db.commit()
    except Exception:  # pragma: no cover - defensive
        db.rollback()
        logger.exception("Access log write failed; continuing.")


def note_session(db: Session, user: models.User, request) -> None:
    """A session made a request. Thinned to one row per day per address."""
    try:
        today = analytics._today()
        ip = _client_ip(request)
        with _guard:
            if _last_session.get(user.id) == (today, ip):
                return
            _last_session[user.id] = (today, ip)
            if len(_last_session) > _MAX_TRACKED:
                # Nothing here is worth persisting; dropping the map costs at
                # most one extra row per active user.
                _last_session.clear()
                _last_session[user.id] = (today, ip)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Access log session check failed; continuing.")
        return
    record(db, SESSION, request, user=user)


def recent(
    db: Session,
    *,
    limit: int = 50,
    before_id: int | None = None,
    kind: str | None = None,
    query: str | None = None,
) -> dict:
    """A page of the log, newest first.

    Anchored on a row id rather than an offset, like the story pager: rows keep
    arriving while it is being read, and an offset would shift the page under
    whoever is reading it.
    """
    statement = select(models.AccessEvent).order_by(desc(models.AccessEvent.id))
    if before_id is not None:
        statement = statement.where(models.AccessEvent.id < before_id)
    if kind:
        statement = statement.where(models.AccessEvent.kind == kind)
    if query:
        like = f"%{query.strip()}%"
        statement = statement.where(or_(
            models.AccessEvent.who.ilike(like),
            models.AccessEvent.ip.ilike(like),
            models.AccessEvent.country.ilike(like),
        ))
    # One extra row answers "is there more" without a second COUNT over the
    # whole table.
    rows = list(db.scalars(statement.limit(limit + 1)))
    has_more = len(rows) > limit
    return {"events": rows[:limit], "has_more": has_more}
