"""Retention policy for throwaway guest accounts.

In multi-user mode every first visit creates a `users` row through
`GET /api/auth/me`, so a public demo accumulates one account per visitor. Most
of those visitors never return, and each one leaves behind whatever scenarios,
adventures, actions, and memories they generated. This module deletes guests
that have been inactive for `AIDND_GUEST_RETENTION_DAYS`, which defaults to 5,
along with everything they made.

Why this is safe to run unattended:

- Only rows with `is_guest` AND `email IS NULL` are ever touched, and both
  clauses are checked rather than either alone. Registering upgrades the row
  in place (is_guest -> False), so a guest who signs up keeps everything;
  local mode's implicit single user is also is_guest=False.
- Idle time is `COALESCE(last_seen_at, created_at)`. `auth._touch` writes
  `last_seen_at` at most once an hour, and a guest created by `/auth/me` has
  NULL there until its second request, so `created_at` is the correct floor for
  a new visitor. Without the coalesce, those rows look arbitrarily old.
- Nothing a guest owns is reachable by anyone else. `is_public` is an
  output-only field, as `schemas.ScenarioBase` shows, so the only shared
  scenarios are the seeded ones, which have a NULL `user_id` and are outside
  this filter. Deleting a guest cannot remove content from another user.

The sweep uses one Core DELETE rather than an ORM cascade. `db.delete(user)`
would SELECT every adventure, action, memory, and story card into Python only to
delete them, which on Neon is the egress pattern that has already cost this
project once. Every foreign key from `users` downward is ON DELETE CASCADE, from
users to scenarios, adventures, scripts, and settings, and from those to actions,
memories, and cards, so the database deletes the whole graph in one statement and
returns a row count.

The scan gets no index. The sweep runs a few times a day against a table holding
at most a few thousand rows, which does not justify a migration and the schema
surface it adds.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import delete, func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import analytics, auth, models
from .database import SessionLocal

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("%s is not an integer; using %d.", name, default)
        return default


# Days of inactivity before a guest account is deleted. A value of 0 or less
# disables the policy, for a deployment that keeps everything.
RETENTION_DAYS = _int_env("AIDND_GUEST_RETENTION_DAYS", 5)

# How often a long-lived process re-checks. Hours, not minutes: nothing here is
# time-critical, and on Render's free tier the service sleeps and cold-starts
# often enough that the startup sweep does most of the work by itself.
SWEEP_INTERVAL_SECONDS = _int_env("AIDND_CLEANUP_INTERVAL_HOURS", 6) * 3600


def enabled() -> bool:
    """Guests only exist in multi-user mode, so local runs skip the sweep
    rather than pointing a DELETE at a database that has nothing to collect."""
    return auth.MULTI_USER and RETENTION_DAYS > 0


def anything_to_sweep() -> bool:
    """Whether the periodic task is worth starting at all. The two jobs it runs
    are independent: a deployment can keep every guest forever and still want
    its analytics rows aged out, and vice versa."""
    return enabled() or analytics.RETENTION_DAYS > 0


def delete_stale_guests(db: Session, *, now: datetime | None = None) -> int:
    """Delete guests idle for RETENTION_DAYS or more. Returns the row count.

    The caller owns error handling; `sweep` is the safe wrapper.
    """
    if RETENTION_DAYS <= 0:
        return 0
    # Stored timestamps are UTC without a timezone on both backends. SQLite
    # drops the timezone, and the Postgres columns are TIMESTAMP WITHOUT TIME
    # ZONE with the session pinned to UTC in `database.py`. Match that, so the
    # comparison does not depend on how a dialect renders a value that carries a
    # timezone.
    reference = now or models.utcnow()
    cutoff = reference.replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)

    stmt = (
        delete(models.User)
        .where(
            models.User.is_guest.is_(True),
            models.User.email.is_(None),
            func.coalesce(models.User.last_seen_at, models.User.created_at) < cutoff,
        )
        # Without this option, the "auto" strategy cannot evaluate coalesce in
        # Python and falls back to fetching every matching primary key first.
        # That is a second round trip for no benefit, because this session holds
        # no User objects to synchronize.
        .execution_options(synchronize_session=False)
    )
    removed = db.execute(stmt).rowcount or 0
    db.commit()
    return removed


def sweep() -> int:
    """One pass, with its own session. Never raises: a failed cleanup must not
    be able to take the app down (same rule as seeding). Returns the guest
    count, which is the number worth logging about."""
    if not anything_to_sweep():
        return 0
    db = SessionLocal()
    try:
        # Ages out the per-visitor analytics rows, on its own terms: it is not
        # about guests, and it must still happen on a deployment that has
        # chosen to keep every account it ever minted.
        aged = analytics.purge_old_visitor_days(db)
        if aged:
            logger.info("Aged out %d analytics visitor-day row(s).", aged)
        removed = delete_stale_guests(db) if enabled() else 0
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
    """Kick off the periodic sweep; None when there is nothing to sweep."""
    if not enabled():
        logger.info("Guest cleanup disabled (multi_user=%s, retention_days=%d).",
                    auth.MULTI_USER, RETENTION_DAYS)
    else:
        logger.info(
            "Guest cleanup on: deleting guests idle %d+ days, every %d hour(s).",
            RETENTION_DAYS,
            SWEEP_INTERVAL_SECONDS // 3600,
        )
    if not anything_to_sweep():
        return None
    return asyncio.create_task(_sweep_loop())


async def stop_sweeper(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
