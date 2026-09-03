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

A table of arrivals raises two questions it cannot answer. `person` answers the
first, which is what one of these people came for: it gathers the rows about a
single user and joins them to what that user played. `devices_seen` answers the
second, which is what they all arrive in, by reading the stored user-agent
strings back as browsers and systems to test on.
"""

import logging
import threading

from sqlalchemy import desc, func, or_, select
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


def _iso(value) -> str | None:
    """Returns a timestamp from an aggregate as an ISO string.

    `func.max` over a DateTime column returns a datetime on PostgreSQL and a
    string on SQLite, because SQLite has no date type and SQLAlchemy cannot
    infer one through the function. Both are already ISO here; only the datetime
    needs converting.
    """
    if not value:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _snapshot(db: Session, user_id: int):
    """Returns the newest log row for a user, which is the only description of
    them that survives the account."""
    return db.scalars(
        select(models.AccessEvent)
        .where(models.AccessEvent.user_id == user_id)
        .order_by(desc(models.AccessEvent.id))
        .limit(1)
    ).first()


def _describe_gone(snapshot, user_id: int) -> str:
    """Returns how to name a user whose account no longer exists."""
    return snapshot.who if snapshot and snapshot.who else f"User #{user_id}"


def _is_guest(user, snapshot) -> bool:
    """Whether this person is a guest, from the account or from the log."""
    if user is not None:
        return bool(user.is_guest)
    return bool(snapshot.is_guest) if snapshot is not None else False


def _demo_turns_today(user) -> int:
    """Turns they have taken on the shared demo key today, and 0 on any other
    day: the stored tally is reset lazily, by the next turn."""
    if user is None or user.demo_turns_date != analytics._today():
        return 0
    return int(user.demo_turns_used)

def person(db: Session, user_id: int) -> dict:
    """Returns what one person in the log has done, or {} when nobody matches.

    The table answers who arrived. This answers what they came back for: how
    many adventures they started, how many turns they played, which shared
    scenarios they chose, and the addresses and devices those arrivals came
    from. The log half comes from this module's own table, the play half from
    their adventures.

    Two things are deliberately absent. Adventure titles and any text they
    wrote are their story, not their record, so nothing here reads a story, and
    the scenario list names only scenarios everyone can see. A failed sign-in
    has no account behind it and so has no entry here, which is the one case
    that returns {} while the row itself stays in the table.

    An account that guest cleanup has since deleted does have an entry. The log
    outlives the account by design, so the answer becomes "gone, and this is
    what they did while they were here" rather than an error.
    """
    event = models.AccessEvent
    seen = db.execute(
        select(func.count(), func.min(event.at), func.max(event.at))
        .where(event.user_id == user_id)
    ).one()
    user = db.get(models.User, user_id)
    if not seen[0] and user is None:
        return {}
    # When the account is gone, the newest row is the only description of them
    # that is left. Guest cleanup deletes accounts on a schedule, so this is the
    # ordinary case for anyone who visited once and never registered.
    snapshot = _snapshot(db, user_id) if user is None else None

    # The register row, of which an account has at most one, is the only record
    # of when they stopped being a guest.
    registered_at = db.scalar(
        select(func.max(event.at))
        .where(event.user_id == user_id, event.kind == REGISTER)
    )
    kinds = {
        kind: int(count)
        for kind, count in db.execute(
            select(event.kind, func.count())
            .where(event.user_id == user_id)
            .group_by(event.kind)
        )
    }
    # One row per address the person has arrived from, the eight most recent
    # first. A visitor on a phone and a laptop, or one who moved networks, is
    # the reason this is a list rather than a single last-seen address.
    places = [
        {
            "ip": ip,
            "country": country,
            "hits": int(count),
            "last_at": _iso(last),
        }
        for ip, country, count, last in db.execute(
            select(event.ip, event.country, func.count(), func.max(event.at))
            .where(event.user_id == user_id)
            .group_by(event.ip, event.country)
            .order_by(desc(func.max(event.at)))
            .limit(8)
        )
    ]
    # One row per browser they have arrived in, newest first. "Phone" is the
    # answer the counters give; this is the answer someone testing the app
    # needs, which is which browser, on which system, on what kind of machine.
    devices = [
        {
            "device": device,
            "browser": analytics.browser_of(agent),
            "platform": analytics.platform_of(agent),
            "hits": int(count),
            "last_at": _iso(last),
            "user_agent": agent,
        }
        for agent, device, count, last in db.execute(
            select(event.user_agent, event.device, func.count(), func.max(event.at))
            .where(event.user_id == user_id)
            .group_by(event.user_agent, event.device)
            .order_by(desc(func.max(event.at)))
            .limit(8)
        )
    ]

    adventure = models.Adventure
    started, first_started = db.execute(
        select(func.count(), func.min(adventure.created_at))
        .where(adventure.user_id == user_id)
    ).one()
    # A turn is one AI reply, counted the same way the dashboard counts it. This
    # number includes retries, because a retry is a turn that was played and
    # paid for even when the reply was thrown away. The newest of those replies
    # is when they last played, which `Adventure.updated_at` is not: editing an
    # adventure's memory moves that column without a turn being played.
    turns, last_played = db.execute(
        select(func.count(), func.max(models.Action.created_at))
        .select_from(models.Action)
        .join(adventure, models.Action.adventure_id == adventure.id)
        .where(adventure.user_id == user_id, models.Action.type == "ai")
    ).one()
    scenarios = [
        {"title": title, "adventures": int(count)}
        for title, count in db.execute(
            select(models.Scenario.title, func.count())
            .join(adventure, adventure.scenario_id == models.Scenario.id)
            .where(adventure.user_id == user_id, models.Scenario.is_public.is_(True))
            .group_by(models.Scenario.title)
            .order_by(desc(func.count()))
            .limit(6)
        )
    ]
    # Their own scenarios are counted but never named, for the reason in the
    # docstring: a scenario they wrote is their writing.
    own_scenarios = db.scalar(
        select(func.count())
        .select_from(models.Scenario)
        .where(models.Scenario.user_id == user_id, models.Scenario.is_public.is_(False))
    )

    return {
        "user_id": user_id,
        "who": describe(user) if user is not None else _describe_gone(snapshot, user_id),
        "is_guest": _is_guest(user, snapshot),
        "account_exists": user is not None,
        # When the row was created, which for a registered account is their
        # first visit as a guest: registration upgrades the row in place. The
        # date they registered is the log's own `register` row, above.
        "account_since": _iso(user.created_at) if user is not None else None,
        "last_seen_at": _iso(user.last_seen_at) if user is not None else None,
        # The tally resets on the first turn taken on a new UTC day, so a stored
        # count from an earlier day is spent, not owed.
        "demo_turns_today": _demo_turns_today(user),
        "adventures": int(started or 0),
        "turns": int(turns or 0),
        "first_adventure_at": _iso(first_started),
        "last_played_at": _iso(last_played),
        "scenarios": scenarios,
        "own_scenarios": int(own_scenarios or 0),
        "log": {
            "rows": int(seen[0]),
            "first_at": _iso(seen[1]),
            "last_at": _iso(seen[2]),
            "sessions": kinds.get(SESSION, 0),
            "logins": kinds.get(LOGIN, 0),
            "registered_at": _iso(registered_at),
        },
        "places": places,
        "devices": devices,
    }


# How many (person, browser) groups `devices_seen` reads. One person on one
# browser is one group, so this is people times the browsers they use, and the
# cap is a ceiling on a query that would otherwise grow with the log forever.
MAX_DEVICE_GROUPS = 2000


def devices_seen(db: Session, *, limit: int = 12) -> list[dict]:
    """Returns the browsers the log has seen, the ones most people use first.

    This is the list to test against. Rows are grouped by what the strings mean
    rather than by the strings themselves, so a person who updated Chrome twice
    is one row and not three, and `people` counts each person once however many
    times they came back.

    Failed sign-ins have no account behind them. Their browser still counts as
    an arrival, because it is a browser that reached the site, but it counts
    nobody.
    """
    event = models.AccessEvent
    seen: dict[tuple[str, str, str], dict] = {}
    for user_id, agent, device, hits, last in db.execute(
        select(
            event.user_id,
            event.user_agent,
            event.device,
            func.count(),
            func.max(event.at),
        )
        .group_by(event.user_id, event.user_agent, event.device)
        .order_by(desc(func.max(event.at)))
        .limit(MAX_DEVICE_GROUPS)
    ):
        key = (analytics.browser_of(agent), analytics.platform_of(agent), device or "")
        row = seen.get(key)
        if row is None:
            row = seen[key] = {
                "browser": key[0],
                "platform": key[1],
                "device": key[2],
                "people": set(),
                "hits": 0,
                "last_at": None,
            }
        if user_id is not None:
            row["people"].add(user_id)
        row["hits"] += int(hits)
        when = _iso(last)
        # Both come from the same database, so both are the same shape and
        # sort as text.
        if when and (row["last_at"] is None or when > row["last_at"]):
            row["last_at"] = when
    ranked = sorted(seen.values(), key=lambda row: (-len(row["people"]), -row["hits"]))
    return [{**row, "people": len(row["people"])} for row in ranked[:limit]]
