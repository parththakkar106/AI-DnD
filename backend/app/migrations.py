"""Lightweight versioned schema migrations.

How it works:
- A fresh database is created by `Base.metadata.create_all()` (always current)
  and stamped with LATEST_VERSION.
- An existing database runs every migration whose version is greater than its
  stored version, in order, then is stamped.

The version lives in SQLite's PRAGMA user_version, or a one-row
`schema_version` table on Postgres (no PRAGMA there).

To change the schema: update models.py (keeps fresh DBs current) AND append a
(version, sql) pair here (upgrades existing DBs). Keep migrations idempotent
where cheap (IF NOT EXISTS etc.). Migrations up to 23 predate Postgres support
and use SQLite-only syntax — that's fine because every Postgres database
starts fresh (created by create_all, stamped LATEST, never replays them), but
migrations added from Phase 9 on must run on both dialects.
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
    # Phase 8: optional accounts. The `users` table itself comes from
    # create_all; these adopt all pre-existing rows under a "local user"
    # (id=1) so a single-user install keeps working unchanged.
    (13, """
        INSERT INTO users (id, email, password_hash, is_guest, created_at,
                           demo_turns_used, demo_turns_date)
        SELECT 1, NULL, NULL, 0, CURRENT_TIMESTAMP, 0, ''
        WHERE NOT EXISTS (SELECT 1 FROM users)
    """),
    (14, "ALTER TABLE scenarios ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    (15, "UPDATE scenarios SET user_id = 1"),
    (16, "ALTER TABLE scenarios ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT 0"),
    (17, "ALTER TABLE scripts ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    (18, "UPDATE scripts SET user_id = 1"),
    (19, "ALTER TABLE adventures ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    (20, "UPDATE adventures SET user_id = 1"),
    (21, "ALTER TABLE settings ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE"),
    (22, "UPDATE settings SET user_id = 1"),
    (23, "CREATE UNIQUE INDEX IF NOT EXISTS ix_settings_user_id ON settings (user_id)"),
    # Link each adventure-script copy back to its library Script so it can be
    # re-synced on demand. NULL for copies made before this column existed.
    (24, "ALTER TABLE adventure_scripts ADD COLUMN source_script_id INTEGER "
         "REFERENCES scripts(id) ON DELETE SET NULL"),
]

LATEST_VERSION = max((v for v, _ in MIGRATIONS), default=1)


def _get_version(conn) -> int:
    if conn.dialect.name == "sqlite":
        return conn.execute(text("PRAGMA user_version")).scalar() or 1
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    ))
    version = conn.execute(text("SELECT version FROM schema_version")).scalar()
    # A non-fresh database with no stamp can only have been created by an
    # earlier create_all of this same codebase — i.e. already at LATEST.
    return version if version is not None else LATEST_VERSION


def _set_version(conn, version: int) -> None:
    if conn.dialect.name == "sqlite":
        conn.execute(text(f"PRAGMA user_version = {version}"))
        return
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    ))
    if conn.execute(text("SELECT version FROM schema_version")).scalar() is None:
        conn.execute(
            text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": version}
        )
    else:
        conn.execute(text("UPDATE schema_version SET version = :v"), {"v": version})


def bootstrap(engine: Engine) -> None:
    fresh = not inspect(engine).get_table_names()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        if fresh:
            _set_version(conn, LATEST_VERSION)
            return
        current = _get_version(conn)
        for version, sql in MIGRATIONS:
            if version > current:
                conn.execute(text(sql))
                current = version
        _set_version(conn, current)
        _encrypt_plaintext_api_keys(conn)


def _encrypt_plaintext_api_keys(conn) -> None:
    """Phase 8 data migration (can't be plain SQL): API keys saved before
    encryption-at-rest existed are stored bare; wrap them in Fernet. Runs on
    every start but matches nothing once all rows carry the enc: prefix."""
    from . import security  # deferred: security derives its key from DB_PATH setup

    rows = conn.execute(text(
        "SELECT id, api_key FROM settings WHERE api_key != '' AND api_key NOT LIKE 'enc:%'"
    )).all()
    for row_id, plain in rows:
        conn.execute(
            text("UPDATE settings SET api_key = :key WHERE id = :id"),
            {"key": security.encrypt_secret(plain), "id": row_id},
        )
