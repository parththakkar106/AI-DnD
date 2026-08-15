"""Retention policy for throwaway guest accounts.

In multi-user mode every first visit mints a `users` row (GET /api/auth/me),
so a public demo accumulates one account per curious visitor — most of whom
never come back, each leaving behind whatever scenarios, adventures, actions
and memories they generated. This drops guests that have gone quiet for
AIDND_GUEST_RETENTION_DAYS (default 5) along with everything they made.

Why this is safe to run unattended:

- Only rows with `is_guest` AND `email IS NULL` are ever touched, and both
  clauses are checked rather than either alone. Registering upgrades the row
  in place (is_guest -> False), so a guest who signs up keeps everything;
  local mode's implicit single user is also is_guest=False.
- Idle time is COALESCE(last_seen_at, created_at). `auth._touch` only writes
  last_seen_at once an hour, and a guest minted by /auth/me has NULL until
  its *second* request, so created_at is the honest floor for a brand-new
  visitor — without the coalesce those rows look infinitely old.
- Nothing a guest owns is reachable by anyone else: `is_public` is an
  output-only field (see schemas.ScenarioBase), so only seeded scenarios —
  which have user_id NULL and are therefore outside this filter entirely —
  are shared. Deleting a guest can't take content away from another user.

Why one Core DELETE instead of an ORM cascade: `db.delete(user)` would SELECT
every adventure, action, memory and story card into Python purely to delete
them, which on Neon is exactly the egress pattern that has already cost this
project once. Every foreign key from users downwards is ON DELETE CASCADE
(users -> scenarios/adventures/scripts/settings -> actions/memories/cards), so
the database does the whole graph in one statement and ships back a row count.

No index is added for the scan: the sweep runs a handful of times a day
against a table with at most a few thousand rows, which is not worth a
migration and the schema surface that comes with it.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import delete, func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import auth, models
from .database import SessionLocal

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("%s is not an integer; using %d.", name, default)
        return default


# Days of inactivity before a guest account is dropped. 0 or less disables the
# policy entirely, for a deployment that would rather keep everything.
RETENTION_DAYS = _int_env("AIDND_GUEST_RETENTION_DAYS", 5)

# How often a long-lived process re-checks. Hours, not minutes: nothing here is
# time-critical, and on Render's free tier the service sleeps and cold-starts
# often enough that the startup sweep does most of the work by itself.
SWEEP_INTERVAL_SECONDS = _int_env("AIDND_CLEANUP_INTERVAL_HOURS", 6) * 3600


def enabled() -> bool:
    """Guests only exist in multi-user mode, so local runs skip the sweep
    rather than pointing a DELETE at a database that has nothing to collect."""
    return auth.MULTI_USER and RETENTION_DAYS > 0


def delete_stale_guests(db: Session, *, now: datetime | None = None) -> int:
    """Delete guests idle for RETENTION_DAYS or more. Returns the row count.

    The caller owns error handling; `sweep` is the safe wrapper.
    """
    if RETENTION_DAYS <= 0:
        return 0
    # Stored timestamps are naive UTC on both backends (SQLite drops tzinfo;
    # Postgres columns are TIMESTAMP WITHOUT TIME ZONE with the session pinned
    # to UTC in database.py). Match that exactly so the comparison can't hinge
    # on how a given dialect renders an aware value.
    reference = now or models.utcnow()
    cutoff = reference.replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)

    stmt = (
        delete(models.User)
        .where(
            models.User.is_guest.is_(True),
            models.User.email.is_(None),
            func.coalesce(models.User.last_seen_at, models.User.created_at) < cutoff,
        )
        # Without this, "auto" can't evaluate coalesce in Python and falls back
        # to fetching every matching primary key first — a second round trip
        # for nothing, since this session holds no User objects to synchronize.
        .execution_options(synchronize_session=False)
    )
    removed = db.execute(stmt).rowcount or 0
    db.commit()
    return removed


def sweep() -> int:
    """One pass, with its own session. Never raises: a failed cleanup must not
    be able to take the app down (same rule as seeding)."""
    if not enabled():
        return 0
    db = SessionLocal()
    try:
        removed = delete_stale_guests(db)
        if removed:
            logger.info(
                "Cleaned up %d guest account(s) idle for %d+ days.",
                removed,
                RETENTION_DAYS,
            )
        return removed
    except Exception:
        db.rollback()
        logger.exception("Guest cleanup failed; continuing without it.")
        return 0
    finally:
        db.close()


async def _sweep_loop() -> None:
    while True:
        # Blocking DB work: keep it off the event loop, which is also serving
        # SSE turn streams.
        await run_in_threadpool(sweep)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


def start_sweeper() -> asyncio.Task | None:
    """Kick off the periodic sweep; None when the policy is off."""
    if not enabled():
        logger.info("Guest cleanup disabled (multi_user=%s, retention_days=%d).",
                    auth.MULTI_USER, RETENTION_DAYS)
        return None
    logger.info(
        "Guest cleanup on: deleting guests idle %d+ days, every %d hour(s).",
        RETENTION_DAYS,
        SWEEP_INTERVAL_SECONDS // 3600,
    )
    return asyncio.create_task(_sweep_loop())


async def stop_sweeper(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
