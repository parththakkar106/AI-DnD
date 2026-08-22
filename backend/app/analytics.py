"""Visit analytics for the hosted demo.

A small self-hosted counter answering "did anyone visit, and did they play?",
built into the app rather than bolted on with a third-party script: the CSP in
main.py allows scripts from 'self' only, adblockers eat the popular trackers,
and none of them can see the things actually worth knowing here (turns taken,
demo-key spend, which seeded scenario people pick).

Three rules shaped it:

1. **Nothing personal is stored.** No IP addresses, no user agents, no user
   ids, no title of anything a player wrote. A visitor appears only as an HMAC
   of their user id — one-way and salted with the app's secret key, so these
   tables cannot be joined back to an account even by someone holding the
   database. Story content never reaches this module at all. What a *specific*
   person did is deliberately unanswerable; only totals are.
2. **Egress is the budget.** Neon bills for bytes leaving the database and this
   project has already paid for forgetting that once. So counts are aggregated
   in memory and flushed as UPSERTs — a visit is a write, never a read — and
   every dashboard query is a GROUP BY returning tens of rows, never per-visit
   rows. A month of traffic costs a few kilobytes to read back.
3. **The numbers are the server's, not the browser's.** The client reports one
   thing: which page was viewed. Everything that *means* something ("a turn
   happened", "an account was created") is recorded by the code that does it,
   where it can be neither faked by a stranger nor blocked by an extension.

Storage is two tables, both bounded. `analytics_daily` is one row per (day,
metric, label) counter — a few dozen a day. `analytics_visitor_days` is one row
per visitor per day carrying the funnel flags, which is what makes the funnel
count *people* rather than clicks; it is the only table that grows with traffic
and cleanup ages it out.
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
# `metric` is the family, `label` the bucket within it. One generic counter
# table beats a column per thing measured: adding a new question later is a
# constant, not a migration.

M_PAGE = "pageview"
M_EVENT = "event"
M_REFERRER = "referrer"
M_DEVICE = "device"
M_COUNTRY = "country"
M_SCENARIO = "scenario"      # which seeded/public scenario got played
M_ERROR = "error"            # "<status> <route>" for 4xx/5xx on /api

EV_SCENARIO_OPEN = "scenario_opened"
EV_ADVENTURE = "adventure_created"
EV_IMPORT = "adventure_imported"
EV_TURN = "turn"
EV_DEMO_TURN = "demo_turn"   # a turn billed to the shared demo key
EV_TURN_ERROR = "turn_error"
EV_SIGNUP = "signup"
EV_LOGIN = "login"

# Events that are also funnel steps: recording one flips a flag on the
# visitor's row for the day, so the funnel counts distinct people-days instead
# of repeat clicks. The name -> column map is the whole definition of the
# funnel; the dashboard reads it back in this order.
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
# Everything below exists so a hostile visitor can add rows to these tables no
# faster than an honest one. The only label a client can influence is the
# referrer, and these caps together mean the worst it can do is fill one day's
# referrer list with junk and then be folded into "(other)".

MAX_LABEL_LEN = 80
MAX_LABELS_PER_METRIC = 200   # distinct labels per metric per day, then OTHER
MAX_PENDING = 4000            # buffered entries before an inline flush
FLUSH_INTERVAL_SECONDS = 60

# How long the per-visitor-day rows are kept. The daily counters are tiny and
# kept forever; these are the ones that scale with traffic. A visitor whose
# last visit falls off the end counts as new again — a fair trade at this
# horizon, and it keeps the table from being a permanent record of anyone.
RETENTION_DAYS = int(os.environ.get("AIDND_ANALYTICS_RETENTION_DAYS", "400") or 400)

_HOST_OK = re.compile(r"^[a-z0-9.-]+$")
_COUNTRY_OK = re.compile(r"^[A-Z]{2}$")
_NUMERIC_SEGMENT = re.compile(r"^\d+$")

# SPA routes, in the shape the dashboard should show them. Anything else a
# client claims to have viewed becomes OTHER, so the page list can neither be
# polluted nor accidentally record which adventure someone is reading.
KNOWN_ROUTES = {
    "/", "/adventures", "/scenarios", "/scenarios/:id", "/play/:id",
    "/scripts", "/scripts/:id", "/settings", "/chat", "/analytics",
}

# ---------- In-process buffer ----------
# Single-process deployment (same assumption as limits.py), so a plain dict
# under a lock is the whole design. Losing up to a minute of counts to a hard
# restart is acceptable for traffic numbers, and the flusher also runs on
# shutdown; on Render's free tier the service is idle when it sleeps, so the
# buffer it sleeps on is empty anyway.

_counts: dict[tuple[str, str, str], int] = {}
_visits: dict[tuple[str, str], set[str]] = {}       # (day, visitor) -> flags
_labels_seen: dict[tuple[str, str], set[str]] = {}  # (day, metric) -> labels
_guard = threading.Lock()


def _today() -> str:
    return models.utcnow().date().isoformat()


def record(metric: str, label: str = "", *, n: int = 1) -> None:
    """Add `n` to one counter. Never raises: analytics must not be able to
    fail a request it is only watching."""
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
    """A stable but one-way handle for one visitor.

    HMAC of the user id under the app's secret key. Stable, so a returning
    visitor can be told from a new one; one-way, so nothing in the analytics
    tables points back at an account; keyed, so a client cannot compute one and
    claim to be somebody else. One consequence worth knowing: rotating
    AIDND_SECRET_KEY makes every returning visitor look new again.
    """
    digest = hmac.new(security.SECRET_KEY, f"visitor:{user.id}".encode(), sha256)
    return digest.hexdigest()[:32]


def record_visit(user: models.User | None, *, flag: str | None = None) -> None:
    """Note that this visitor was here today, optionally flipping one funnel
    flag. A no-op without a user: a page loaded before a session exists still
    counts as a pageview, just not as a person."""
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
    """One thing that happened: counted, and — if it is a funnel step —
    credited to the visitor's day. This is the call sites' whole interface."""
    record(M_EVENT, name)
    record_visit(user, flag=FUNNEL_FLAGS.get(name))


# ---------- Normalizing what the browser reports ----------

def normalize_route(path: str) -> str:
    """A client-reported path, reduced to one of KNOWN_ROUTES.

    Numeric segments become ":id" — both to bound the label count and because
    *which* adventure someone opened is their business, not a statistic.
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
    """The sending site as a bare host. Our own host means an internal
    navigation, which is not a referral — "" tells the caller to skip it."""
    if not referrer:
        return NONE_LABEL
    host = (urlsplit(referrer).hostname or "").lower().lstrip(".")
    if not host or not _HOST_OK.match(host) or len(host) > MAX_LABEL_LEN:
        return OTHER
    if host == (own_host or "").lower() or host in ("localhost", "127.0.0.1"):
        return ""
    return host[4:] if host.startswith("www.") else host


def api_route_label(scope: dict, status: int) -> str:
    """An error bucket like "500 /api/adventures/{adventure_id}".

    The route *template* is used, never the request path: it keeps one bucket
    per endpoint instead of one per adventure id, and — the reason it is not
    merely tidier — an unmatched path is entirely attacker-chosen, so labelling
    by it would let anyone mint rows by requesting nonsense.
    """
    template = getattr(scope.get("route"), "path", None)
    return f"{status} {template}" if template else f"{status} (unmatched)"


def device_of(user_agent: str) -> str:
    """Mobile / tablet / desktop, and nothing finer. The UA string itself is
    never stored — it is a fingerprint, and the answer worth having is one
    word."""
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


# Geo headers an edge may add. Render fronts services with a CDN that can set
# cf-ipcountry; the others cost nothing to look for. A value is trusted only if
# it looks like an ISO code, since a client can send any header it likes — the
# worst case is therefore a wrong country, never an unbounded label.
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
        # The label sets only bound cardinality within a day, so let yesterday's
        # go rather than growing a map that never shrinks.
        today = _today()
        for key in [k for k in _labels_seen if k[0] != today]:
            del _labels_seen[key]
    return counts, visits


def _restore(counts: dict, visits: dict) -> None:
    """Put a failed flush's work back so the next one retries it."""
    with _guard:
        for key, n in counts.items():
            _counts[key] = _counts.get(key, 0) + n
        for key, flags in visits.items():
            _visits.setdefault(key, set()).update(flags)


def flush(db: Session | None = None) -> None:
    """Write the buffer out. Safe to call from anywhere; never raises."""
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
    # One indexed lookup settles new-vs-returning for the whole batch. It is
    # the only read this module does off the dashboard, and it returns short
    # hashes for visitors who are active right now — bounded by the batch.
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
        # Flags only ever turn on, and `is_new` is deliberately absent: the
        # first write of a visitor's first day is the one that decided it.
        set_={
            column: or_(table.c[column], stmt.excluded[column])
            for column in FUNNEL_FLAGS.values()
        },
    ))


def purge_old_visitor_days(db: Session) -> int:
    """Drop visitor-day rows past the retention horizon. Called by the cleanup
    sweeper; the daily counters are never purged — they are aggregate, tiny,
    and a portfolio project wants to keep its history."""
    if RETENTION_DAYS <= 0:
        return 0
    cutoff = (models.utcnow().date() - timedelta(days=RETENTION_DAYS)).isoformat()
    removed = db.query(models.AnalyticsVisitorDay).filter(
        models.AnalyticsVisitorDay.day < cutoff
    ).delete(synchronize_session=False)
    db.commit()
    return removed or 0


# ---------- Reading it back ----------
# Every query below is an aggregate: the database does the counting and ships
# back tens of rows, whatever the traffic behind them. Nothing here can return
# a row that belongs to one visitor.

TOP_N = 12


def _top(rows: list[dict], limit: int = TOP_N) -> list[dict]:
    return rows[:limit]


def summary(db: Session, days: int = 30) -> dict:
    """Everything the dashboard shows, for the last `days` days (today
    included). Flushes first so the numbers include the last minute."""
    flush(db)
    today = models.utcnow().date()
    since = (today - timedelta(days=days - 1)).isoformat()
    daily = models.AnalyticsDaily
    visitor = models.AnalyticsVisitorDay

    # 1. Every counter in the window, folded to (metric, label) totals: the
    #    page/referrer/country/device/scenario/error tables all come from this
    #    one pass rather than a query each.
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

    # 2. Two per-day series worth drawing.
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

    # 3. People, per day. One row per visitor per day means COUNT(*) is already
    #    the day's unique visitors — no DISTINCT needed here.
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

    # 4. The funnel, over the whole window, counting *people* once each:
    #    COUNT(DISTINCT CASE WHEN flag THEN visitor END) ignores the NULLs the
    #    CASE leaves for everyone who didn't reach that step.
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
            # `visitors` counts each person once for the window; `visits` counts
            # them once per day they came back, which is the closest honest
            # thing to "sessions" without tracking sessions.
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
        # Step 0 is everyone who showed up, so the drop-off between it and
        # "Opened a scenario" is visible as a step like any other.
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
# Mirrors cleanup's start/stop pair so main.py's lifespan reads the same way
# for both. The interval is what bounds how much a hard restart can lose.

async def _flush_loop() -> None:
    import asyncio

    from starlette.concurrency import run_in_threadpool

    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        # Blocking DB work: keep it off the event loop, which is also serving
        # SSE turn streams.
        await run_in_threadpool(flush)


def start_flusher():
    import asyncio

    return asyncio.create_task(_flush_loop())


async def stop_flusher(task) -> None:
    """Cancel the loop and write out whatever it was holding — a deploy is the
    one restart that is both frequent and predictable, so it should not be the
    thing that loses a minute of counts."""
    import asyncio

    from starlette.concurrency import run_in_threadpool

    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await run_in_threadpool(flush)
