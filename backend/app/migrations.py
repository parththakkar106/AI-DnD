"""Lightweight versioned schema migrations over SQLite's PRAGMA user_version.

How it works:
- A fresh database is created by `Base.metadata.create_all()` (always current)
  and stamped with LATEST_VERSION.
- An existing database runs every migration whose version is greater than its
  stored user_version, in order, then is stamped.

To change the schema: update models.py (keeps fresh DBs current) AND append a
(version, sql) pair here (upgrades existing DBs). Keep migrations idempotent
where cheap (IF NOT EXISTS etc.).
"""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .database import Base

# (version, SQL to run when upgrading past it) — append only, never reorder.
MIGRATIONS: list[tuple[int, str]] = [
    # Phase 6: auto-summarization + memory bank (the `memories` table itself is
    # created by create_all, which runs for existing DBs too).
    (2, "ALTER TABLE adventures ADD COLUMN auto_summarize BOOLEAN NOT NULL DEFAULT 0"),
    (3, "ALTER TABLE adventures ADD COLUMN memory_bank_enabled BOOLEAN NOT NULL DEFAULT 0"),
    (4, "ALTER TABLE adventures ADD COLUMN memory_cursor INTEGER NOT NULL DEFAULT 0"),
    (5, "ALTER TABLE adventures ADD COLUMN summary_cursor INTEGER NOT NULL DEFAULT 0"),
    (6, "ALTER TABLE settings ADD COLUMN summary_model VARCHAR(200) NOT NULL DEFAULT ''"),
    (7, "ALTER TABLE settings ADD COLUMN embedding_model VARCHAR(200) NOT NULL DEFAULT ''"),
    (8, "ALTER TABLE settings ADD COLUMN memory_bank_capacity INTEGER NOT NULL DEFAULT 200"),
    (9, "ALTER TABLE settings ADD COLUMN memory_top_k INTEGER NOT NULL DEFAULT 5"),
    # Repair duplicate action indexes (player + AI actions of one turn used to
    # get the same index): renumber 0..n-1 per adventure, preserving order.
    # UPDATE..FROM: ranks are computed as a snapshot before any row is
    # rewritten (a correlated subquery would see partially-updated rows and
    # could produce duplicates again).
    (10, """
        UPDATE actions SET "index" = ranked.new_index
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY adventure_id ORDER BY "index", id
            ) - 1 AS new_index
            FROM actions
        ) AS ranked
        WHERE ranked.id = actions.id
    """),
    # Reasoning-model support: separate thinking budget + stored reasoning text.
    (11, "ALTER TABLE settings ADD COLUMN reasoning_max_tokens INTEGER NOT NULL DEFAULT 0"),
    (12, "ALTER TABLE actions ADD COLUMN reasoning TEXT"),
]

LATEST_VERSION = max((v for v, _ in MIGRATIONS), default=1)


def bootstrap(engine: Engine) -> None:
    fresh = not inspect(engine).get_table_names()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        if fresh:
            conn.execute(text(f"PRAGMA user_version = {LATEST_VERSION}"))
            return
        current = conn.execute(text("PRAGMA user_version")).scalar() or 1
        for version, sql in MIGRATIONS:
            if version > current:
                conn.execute(text(sql))
                current = version
        conn.execute(text(f"PRAGMA user_version = {current}"))
