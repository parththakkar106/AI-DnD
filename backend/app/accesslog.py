"""The access log: who arrived, when, and from where.

The deliberate opposite of analytics.py. That module counts and stores nothing
that points at a person; this one records addresses, email addresses and
devices, because an access log that cannot identify the access is not an access
log. The two live in separate modules and separate tables on purpose, so that
the anonymity of the counters is a property of the code rather than a convention
someone has to remember.

Owner-only, and never shown to the people it records.

Four kinds of row:

- `session`      A browser that has a session made a request. For a guest, this
                 is their first visit.
- `login`        An existing account signed in.
- `register`     A guest upgraded to an account.
- `login_failed` A password attempt that did not match, with the address tried.

Session rows are the only ones that need thinning. `/auth/me` runs on every page
load, and one row per load would be noise rather than a log. A row is written
when the day or the address changes for that user. That is the granularity a log
is read at, such as seen on the 3rd from 1.2.3.4, and it still records someone
moving networks during a day.
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
    # This import is deferred. `limits` imports `auth`, which the routers that
    # call this function import, so a module-level import here would create a
    # cycle. The spoof resistance lives in `limits` and must not be
    # reimplemented. A second, looser answer to which address belongs to the
    # client is how one of them ends up trusting a header it should not.
    from . import limits

    return limits.client_ip(request)


def describe(user: models.User) -> str:
    """Returns how a user is named in the log.

    A guest has no email, and their id is the only handle anyone has for them.
    The third case is a local install's implicit single user, who also has no
    email but is the operator rather than a visitor. Naming that user "Guest #1"
    would be wrong in the one row they are certain to read.
    """
    if user.email:
        return user.email
    return f"Guest #{user.id}" if user.is_guest else f"Local user #{user.id}"


def _country(request) -> str:
    """Returns the edge's country header, or "" when there is none.

    The blank differs from the counters' "(unknown)" label. A table column reads
    better as a dash than as a word, and an empty string is the correct value for
    a country that is not known.
    """
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
    """Writes one row.

    This function never raises. The log observes sign-in rather than guarding it,
    and a logging failure must not lock anyone out.
    """
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
    """Records that a session made a request, at most one row per day per address."""
    try:
        today = analytics._today()
        ip = _client_ip(request)
        with _guard:
            if _last_session.get(user.id) == (today, ip):
                return
            _last_session[user.id] = (today, ip)
            if len(_last_session) > _MAX_TRACKED:
                # Nothing here needs to persist. Clearing the map costs at most
                # one extra row per active user.
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
    """Returns a page of the log, newest first.

    The page is anchored on a row id rather than an offset, as the story pager
    is. Rows keep arriving while the log is read, and an offset would shift the
    page under whoever is reading it.
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
    # Requesting one extra row reports whether more rows exist, without a
    # second COUNT over the whole table.
    rows = list(db.scalars(statement.limit(limit + 1)))
    has_more = len(rows) > limit
    return {"events": rows[:limit], "has_more": has_more}
