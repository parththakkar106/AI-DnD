"""Visit analytics for the hosted demo.

This is a small self-hosted counter that answers whether anyone visited and
whether they played. It is built into the app rather than added with a
third-party script, because the CSP in `main.py` allows scripts from 'self'
only, ad blockers block the popular trackers, and none of those trackers can see
what is worth knowing here: turns taken, demo-key spend, and which seeded
scenario people pick.

Three rules shape the design:

1. It stores nothing personal. It records no IP addresses, no user agents, no
   user ids, and no title of anything a player wrote. A visitor appears only as
   an HMAC of their user id, which is one-way and salted with the app's secret
   key, so these tables cannot be joined back to an account even by someone
   holding the database. Story content never reaches this module. What one
   specific person did is unanswerable by design, and only totals are
   available.
2. Egress is the budget. Neon bills for bytes leaving the database, and this
   project has already paid for forgetting that once. Counts are therefore
   aggregated in memory and flushed as UPSERTs, so a visit is a write and never
   a read, and every dashboard query is a GROUP BY that returns tens of rows
   rather than per-visit rows. A month of traffic costs a few kilobytes to read
   back.
3. The numbers come from the server, not from the browser. The client reports
   one thing, which is the page that was viewed. Everything with meaning, such
   as a turn happening or an account being created, is recorded by the code that
   performs it, where a stranger cannot fake it and an extension cannot block
   it.

Storage is two tables, both bounded. `analytics_daily` holds one counter row per
day, metric, and label, which is a few dozen rows a day.
`analytics_visitor_days` holds one row per visitor per day carrying the funnel
flags, which is what makes the funnel count people rather than clicks. It is the
only table that grows with traffic, and cleanup ages it out.
"""

import hmac
import logging
import os
import re
import threading
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from . import models, security

logger = logging.getLogger(__name__)

# ---------- Metrics ----------
# `metric` is the family, and `label` is the bucket within it. One generic
# counter table is better than a column per measurement, because adding a new
# question later costs nothing rather than a migration.

M_PAGE = "pageview"
M_EVENT = "event"
M_REFERRER = "referrer"
M_DEVICE = "device"
M_COUNTRY = "country"
M_SCENARIO = "scenario"      # Which seeded or public scenario was played.
M_ERROR = "error"            # "<status> <route>" for a 4xx or 5xx on /api.

EV_SCENARIO_OPEN = "scenario_opened"
EV_ADVENTURE = "adventure_created"
EV_IMPORT = "adventure_imported"
EV_TURN = "turn"
EV_DEMO_TURN = "demo_turn"   # A turn billed to the shared demo key.
EV_TURN_ERROR = "turn_error"
EV_SIGNUP = "signup"
EV_LOGIN = "login"

# Events that are also funnel steps. Recording one sets a flag on the visitor's
# row for the day, so the funnel counts distinct visitor-days rather than repeat
# clicks. This name-to-column map is the whole definition of the funnel, and the
# dashboard reads it back in this order.
FUNNEL_FLAGS = {
    EV_SCENARIO_OPEN: "opened",
    EV_ADVENTURE: "created",
    EV_TURN: "played",
    EV_SIGNUP: "signed_up",
}

OTHER = "(other)"
NONE_LABEL = "(direct)"
UNKNOWN = "(unknown)"

# ---------- Bounds ----------
# These bounds exist so that a hostile visitor can add rows to these tables no
# faster than an honest one. The only label a client can influence is the
# referrer, and together these caps mean the worst it can do is fill one day's
# referrer list and then be folded into "(other)".

MAX_LABEL_LEN = 80
MAX_LABELS_PER_METRIC = 200   # Distinct labels per metric per day, then OTHER.
MAX_PENDING = 4000            # Buffered entries before an inline flush.
FLUSH_INTERVAL_SECONDS = 60

# How long the per-visitor-day rows are kept. The daily counters are small and
# are kept indefinitely. These rows are the ones that scale with traffic. A
# visitor whose last visit ages out counts as new again, which is an acceptable
# trade at this horizon and keeps the table from being a permanent record of
# anyone.
RETENTION_DAYS = int(os.environ.get("AIDND_ANALYTICS_RETENTION_DAYS", "400") or 400)

_HOST_OK = re.compile(r"^[a-z0-9.-]+$")
_COUNTRY_OK = re.compile(r"^[A-Z]{2}$")
_NUMERIC_SEGMENT = re.compile(r"^\d+$")

# SPA routes, in the form the dashboard shows them. Any other path a client
# reports becomes OTHER, so the page list cannot be filled with junk and cannot
# record which adventure someone is reading.
KNOWN_ROUTES = {
    "/", "/adventures", "/scenarios", "/scenarios/:id", "/play/:id",
    "/scripts", "/scripts/:id", "/settings", "/chat", "/analytics",
}

# ---------- In-process buffer ----------
# The deployment is a single process, which is the same assumption `limits.py`
# makes, so a plain dict under a lock is the whole design. Losing up to a minute
# of counts to a hard restart is acceptable for traffic numbers, and the flusher
# also runs on shutdown. On Render's free tier the service is idle when it
# sleeps, so the buffer it sleeps on is empty.

_counts: dict[tuple[str, str, str], int] = {}
_visits: dict[tuple[str, str], set[str]] = {}       # (day, visitor) -> flags.
_labels_seen: dict[tuple[str, str], set[str]] = {}  # (day, metric) -> labels.
_guard = threading.Lock()


def _today() -> str:
    return models.utcnow().date().isoformat()


def record(metric: str, label: str = "", *, n: int = 1) -> None:
    """Adds `n` to one counter.

    This function never raises. Analytics must not fail a request that it is
    only observing.
    """
    try:
        day = _today()
        label = (label or "").strip()[:MAX_LABEL_LEN]
        with _guard:
            seen = _labels_seen.setdefault((day, metric), set())
            if label not in seen:
                if len(seen) >= MAX_LABELS_PER_METRIC:
                    label = OTHER
                else:
                    seen.add(label)
            key = (day, metric, label)
            _counts[key] = _counts.get(key, 0) + n
            pending = len(_counts) + len(_visits)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Analytics counter failed; continuing.")
        return
    if pending >= MAX_PENDING:
        flush()


def visitor_id(user: models.User) -> str:
    """Returns a stable, one-way handle for one visitor.

    The handle is an HMAC of the user id under the app's secret key. It is
    stable, so a returning visitor can be distinguished from a new one. It is
    one-way, so nothing in the analytics tables points back at an account. It is
    keyed, so a client cannot compute one and claim to be someone else. One
    consequence follows: rotating `AIDND_SECRET_KEY` makes every returning
    visitor look new.
    """
    digest = hmac.new(security.SECRET_KEY, f"visitor:{user.id}".encode(), sha256)
    return digest.hexdigest()[:32]


def record_visit(user: models.User | None, *, flag: str | None = None) -> None:
    """Records that this visitor was here today, and optionally sets one funnel
    flag.

    Without a user the call does nothing. A page loaded before a session exists
    still counts as a pageview, but not as a person.
    """
    if user is None:
        return
    try:
        with _guard:
            flags = _visits.setdefault((_today(), visitor_id(user)), set())
            if flag:
                flags.add(flag)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Analytics visit failed; continuing.")


def record_event(name: str, user: models.User | None = None) -> None:
    """Records one event, and credits the visitor's day if it is a funnel step.

    This is the whole interface the call sites use.
    """
    record(M_EVENT, name)
    record_visit(user, flag=FUNNEL_FLAGS.get(name))


# ---------- Normalizing what the browser reports ----------

def normalize_route(path: str) -> str:
    """Reduces a client-reported path to one of `KNOWN_ROUTES`.

    Numeric segments become ":id". That bounds the label count, and it keeps
    which adventure someone opened out of the statistics.
    """
    path = (path or "/").split("?")[0].split("#")[0]
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    parts = [":id" if _NUMERIC_SEGMENT.match(p) else p for p in path.split("/")]
    route = "/".join(parts) or "/"
    return route if route in KNOWN_ROUTES else OTHER


def normalize_referrer(referrer: str, own_host: str = "") -> str:
    """Returns the sending site as a bare host.

    This app's own host means an internal navigation, which is not a referral.
    In that case the function returns "", which tells the caller to skip it.
    """
    if not referrer:
        return NONE_LABEL
    host = (urlsplit(referrer).hostname or "").lower().lstrip(".")
    if not host or not _HOST_OK.match(host) or len(host) > MAX_LABEL_LEN:
        return OTHER
    if host == (own_host or "").lower() or host in ("localhost", "127.0.0.1"):
        return ""
    return host[4:] if host.startswith("www.") else host


def api_route_label(scope: dict, status: int) -> str:
    """Returns an error bucket such as "500 /api/adventures/{adventure_id}".

    The label uses the route template, never the request path. That keeps one
    bucket per endpoint rather than one per adventure id. It also bounds the
    table: an unmatched path is chosen entirely by the caller, so labeling by it
    would let anyone create rows by requesting arbitrary paths.
    """
    template = getattr(scope.get("route"), "path", None)
    return f"{status} {template}" if template else f"{status} (unmatched)"


def device_of(user_agent: str) -> str:
    """Returns "mobile", "tablet", or "desktop", and nothing more specific.

    The user-agent string itself is never stored, because it is a fingerprint
    and the useful answer is one word.
    """
    ua = (user_agent or "").lower()
    if not ua:
        return UNKNOWN
    if any(bot in ua for bot in ("bot", "crawler", "spider", "headless", "preview")):
        return "bot"
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua):
        return "tablet"
    if any(m in ua for m in ("mobi", "iphone", "ipod", "android", "phone")):
        return "mobile"
    return "desktop"


# Geo headers an edge network may add. Render fronts services with a CDN that
# can set `cf-ipcountry`, and the others cost nothing to check. A value is
# trusted only if it looks like an ISO code, because a client can send any
# header, so the worst case is a wrong country rather than an unbounded label.
_GEO_HEADERS = ("cf-ipcountry", "x-vercel-ip-country", "x-geo-country", "x-country-code")


def country_of(headers) -> str:
    for name in _GEO_HEADERS:
        value = (headers.get(name) or "").strip().upper()
        if _COUNTRY_OK.match(value) and value != "XX":
            return value
    return UNKNOWN


# ---------- Flushing ----------

def _insert(db: Session):
    return sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert


def _drain() -> tuple[dict, dict]:
    with _guard:
        counts, visits = _counts.copy(), _visits.copy()
        _counts.clear()
        _visits.clear()
        # The label sets bound cardinality within one day, so drop the
        # previous day's rather than grow a map that never shrinks.
        today = _today()
        for key in [k for k in _labels_seen if k[0] != today]:
            del _labels_seen[key]
    return counts, visits


def _restore(counts: dict, visits: dict) -> None:
    """Returns a failed flush's work to the buffer, so the next flush retries it."""
    with _guard:
        for key, n in counts.items():
            _counts[key] = _counts.get(key, 0) + n
        for key, flags in visits.items():
            _visits.setdefault(key, set()).update(flags)


def flush(db: Session | None = None) -> None:
    """Writes the buffer out. This is safe to call from anywhere and never raises."""
    counts, visits = _drain()
    if not counts and not visits:
        return
    own_session = db is None
    if own_session:
        from .database import SessionLocal
        db = SessionLocal()
    try:
        _write_counts(db, counts)
        _write_visits(db, visits)
        db.commit()
    except Exception:
        db.rollback()
        _restore(counts, visits)
        logger.exception("Analytics flush failed; counts held for the next one.")
    finally:
        if own_session:
            db.close()


def _write_counts(db: Session, counts: dict) -> None:
    if not counts:
        return
    table = models.AnalyticsDaily.__table__
    rows = [
        {"day": day, "metric": metric, "label": label, "hits": hits}
        for (day, metric, label), hits in counts.items()
    ]
    stmt = _insert(db)(table).values(rows)
    db.execute(stmt.on_conflict_do_update(
        index_elements=["day", "metric", "label"],
        set_={"hits": table.c.hits + stmt.excluded.hits},
    ))


def _write_visits(db: Session, visits: dict) -> None:
    if not visits:
        return
    table = models.AnalyticsVisitorDay.__table__
    ids = {visitor for _, visitor in visits}
    # One indexed lookup decides new against returning for the whole batch. It
    # is the only read this module makes outside the dashboard, and it returns
    # short hashes for the visitors active right now, so the batch bounds it.
    known = set(db.scalars(
        select(models.AnalyticsVisitorDay.visitor)
        .where(models.AnalyticsVisitorDay.visitor.in_(ids))
        .distinct()
    ))
    rows = [
        {
            "day": day,
            "visitor": visitor,
            "is_new": visitor not in known,
            **{column: column in flags for column in FUNNEL_FLAGS.values()},
        }
        for (day, visitor), flags in visits.items()
    ]
    stmt = _insert(db)(table).values(rows)
    db.execute(stmt.on_conflict_do_update(
        index_elements=["day", "visitor"],
        # Flags only turn on, and `is_new` is absent on purpose. The first
        # write of a visitor's first day is what decided it.
        set_={
            column: or_(table.c[column], stmt.excluded[column])
            for column in FUNNEL_FLAGS.values()
        },
    ))


def purge_old_visitor_days(db: Session) -> int:
    """Deletes visitor-day rows past the retention horizon.

    The cleanup sweeper calls this. The daily counters are never purged, because
    they are aggregates, they are small, and this project keeps its history.
    """
    if RETENTION_DAYS <= 0:
        return 0
    cutoff = (models.utcnow().date() - timedelta(days=RETENTION_DAYS)).isoformat()
    removed = db.query(models.AnalyticsVisitorDay).filter(
        models.AnalyticsVisitorDay.day < cutoff
    ).delete(synchronize_session=False)
    db.commit()
    return removed or 0


# ---------- Reading it back ----------
# Every query below is an aggregate. The database does the counting and returns
# tens of rows, however much traffic is behind them. No query here can return a
# row that belongs to one visitor.

TOP_N = 12


def _top(rows: list[dict], limit: int = TOP_N) -> list[dict]:
    return rows[:limit]


def summary(db: Session, days: int = 30) -> dict:
    """Returns everything the dashboard shows for the last `days` days, including
    today.

    The function flushes first, so the numbers include the last minute.
    """
    flush(db)
    today = models.utcnow().date()
    since = (today - timedelta(days=days - 1)).isoformat()
    daily = models.AnalyticsDaily
    visitor = models.AnalyticsVisitorDay

    # 1. Every counter in the window, reduced to (metric, label) totals. The
    #    page, referrer, country, device, scenario, and error tables all come
    #    from this one pass rather than from a query each.
    by_metric: dict[str, list[dict]] = {}
    for metric, label, hits in db.execute(
        select(daily.metric, daily.label, func.sum(daily.hits))
        .where(daily.day >= since)
        .group_by(daily.metric, daily.label)
    ):
        by_metric.setdefault(metric, []).append({"label": label, "hits": int(hits)})
    for rows in by_metric.values():
        rows.sort(key=lambda row: -row["hits"])
    events = {row["label"]: row["hits"] for row in by_metric.get(M_EVENT, [])}

    # 2. The two per-day series the dashboard draws.
    pageviews_by_day = {
        day: int(hits)
        for day, hits in db.execute(
            select(daily.day, func.sum(daily.hits))
            .where(daily.day >= since, daily.metric == M_PAGE)
            .group_by(daily.day)
        )
    }
    turns_by_day = {
        day: int(hits)
        for day, hits in db.execute(
            select(daily.day, func.sum(daily.hits))
            .where(daily.day >= since, daily.metric == M_EVENT, daily.label == EV_TURN)
            .group_by(daily.day)
        )
    }

    # 3. People, per day. There is one row per visitor per day, so COUNT(*) is
    #    already the day's unique visitors and no DISTINCT is needed.
    visitors_by_day: dict[str, dict] = {}
    for day, total, fresh in db.execute(
        select(
            visitor.day,
            func.count(),
            func.sum(case((visitor.is_new, 1), else_=0)),
        )
        .where(visitor.day >= since)
        .group_by(visitor.day)
    ):
        visitors_by_day[day] = {"visitors": int(total), "new": int(fresh or 0)}

    # 4. The funnel over the whole window, counting each person once.
    #    COUNT(DISTINCT CASE WHEN flag THEN visitor END) ignores the NULLs the
    #    CASE leaves for everyone who did not reach that step.
    unique, unique_new, *reached = db.execute(
        select(
            func.count(func.distinct(visitor.visitor)),
            func.count(func.distinct(case((visitor.is_new, visitor.visitor)))),
            *[
                func.count(func.distinct(case((visitor.__table__.c[column], visitor.visitor))))
                for column in FUNNEL_FLAGS.values()
            ],
        ).where(visitor.day >= since)
    ).one()

    series = []
    for offset in range(days):
        day = (today - timedelta(days=days - 1 - offset)).isoformat()
        counted = visitors_by_day.get(day, {})
        series.append({
            "day": day,
            "visitors": counted.get("visitors", 0),
            "new": counted.get("new", 0),
            "pageviews": pageviews_by_day.get(day, 0),
            "turns": turns_by_day.get(day, 0),
        })

    visits = sum(row["visitors"] for row in series)
    pageviews = sum(pageviews_by_day.values())
    turns = events.get(EV_TURN, 0)
    errors = by_metric.get(M_ERROR, [])
    return {
        "days": days,
        "since": since,
        "until": today.isoformat(),
        "generated_at": models.utcnow().isoformat(),
        "totals": {
            # `visitors` counts each person once for the window. `visits`
            # counts them once per day they returned, which is the closest
            # measure to "sessions" that does not track sessions.
            "visitors": int(unique),
            "new_visitors": int(unique_new),
            "visits": visits,
            "pageviews": pageviews,
            "turns": turns,
            "demo_turns": events.get(EV_DEMO_TURN, 0),
            "adventures": events.get(EV_ADVENTURE, 0),
            "signups": events.get(EV_SIGNUP, 0),
            "logins": events.get(EV_LOGIN, 0),
            "turn_errors": events.get(EV_TURN_ERROR, 0),
            "errors": sum(row["hits"] for row in errors),
            "turns_per_visit": round(turns / visits, 1) if visits else 0,
            "pages_per_visit": round(pageviews / visits, 1) if visits else 0,
        },
        "series": series,
        # Step 0 is everyone who arrived, so the drop-off between it and
        # "Opened a scenario" appears as a step like any other.
        "funnel": [{"step": "Visited", "count": int(unique)}] + [
            {"step": step, "count": int(count)}
            for step, count in zip(
                ["Opened a scenario", "Started an adventure", "Played a turn", "Signed up"],
                reached,
            )
        ],
        "pages": _top(by_metric.get(M_PAGE, [])),
        "referrers": _top(by_metric.get(M_REFERRER, [])),
        "countries": _top(by_metric.get(M_COUNTRY, [])),
        "devices": by_metric.get(M_DEVICE, []),
        "scenarios": _top(by_metric.get(M_SCENARIO, [])),
        "errors": _top(errors),
        "events": by_metric.get(M_EVENT, []),
    }


# ---------- Background flusher ----------
# This matches the start and stop pair in `cleanup`, so the lifespan in
# `main.py` reads the same way for both. The interval bounds how much a hard
# restart can lose.

async def _flush_loop() -> None:
    import asyncio

    from starlette.concurrency import run_in_threadpool

    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        # This is blocking database work, so keep it off the event loop, which
        # is also serving SSE turn streams.
        await run_in_threadpool(flush)


def start_flusher():
    import asyncio

    return asyncio.create_task(_flush_loop())


async def stop_flusher(task) -> None:
    """Cancels the loop and writes out whatever it was holding.

    A deploy is the one restart that is both frequent and predictable, so it
    should not be what loses a minute of counts.
    """
    import asyncio

    from starlette.concurrency import run_in_threadpool

    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await run_in_threadpool(flush)
