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

import json

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from . import vectors
from .database import Base

# (version, SQL to run when upgrading past it) — append only, never reorder.
# The SQL is a string, or a {dialect: sql} map with a "default" entry when the
# two dialects have to be spelled differently (BLOB vs BYTEA and the like).
MIGRATIONS: list[tuple[int, str | dict[str, str]]] = [
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
    # Per-action snapshot of the shared script_state as it was before that
    # action's hooks ran, enabling undo/retry to roll state back. JSON is valid
    # on both SQLite and Postgres.
    (25, "ALTER TABLE actions ADD COLUMN state_before JSON"),
    # Phase 12: RPG world state. `stat_schema` defines the stats/bands/rules and
    # milestones for a scenario; `world_state` holds an adventure's live values;
    # `world_state_before` snapshots it per action for undo/retry (mirrors
    # state_before). JSON is valid on both SQLite and Postgres.
    (26, "ALTER TABLE scenarios ADD COLUMN stat_schema JSON"),
    (27, "ALTER TABLE adventures ADD COLUMN world_state JSON"),
    (28, "ALTER TABLE actions ADD COLUMN world_state_before JSON"),
    # Raise the default context budget 4096 -> 16384 (Phase 12 injects a stat
    # guide + world state each turn). Only bumps rows still on the old default,
    # so anyone who picked a custom value keeps it.
    (29, "UPDATE settings SET context_token_budget = 16384 WHERE context_token_budget = 4096"),
    # Scenario cover art — an external URL or an inline base64 data URI. TEXT
    # (not VARCHAR) because a downscaled data URI runs tens of kilobytes.
    (30, "ALTER TABLE scenarios ADD COLUMN image TEXT NOT NULL DEFAULT ''"),
    # Emoji/glyph fallback used when `image` is empty.
    (31, "ALTER TABLE scenarios ADD COLUMN icon VARCHAR(16) NOT NULL DEFAULT ''"),
    # The ${Placeholder} answers given when the adventure was started. Kept so
    # "Update from scenario" can re-fill re-copied text; NULL for adventures
    # created before this column, which re-prompt for them on first refresh.
    (32, "ALTER TABLE adventures ADD COLUMN placeholders JSON"),
    # Which piece of the scenario a copied story card came from ("card:<id>" or
    # "npc:<key>"), so a refresh can update/remove exactly the scenario-derived
    # cards and leave player-authored ones alone. NULL = player-authored, or a
    # copy predating this column (matched by name once, then adopted).
    (33, "ALTER TABLE story_cards ADD COLUMN source_ref VARCHAR(64)"),
    # Retry history: every attempt made for an AI turn, oldest first, so retry
    # can append instead of deleting. NULL = never retried (the row is its own
    # only version), which is also the correct reading for every action that
    # predates this column.
    (34, "ALTER TABLE actions ADD COLUMN variants JSON"),
    (35, "ALTER TABLE actions ADD COLUMN variant_index INTEGER NOT NULL DEFAULT 0"),
    # Egress: context_snapshot holds the whole assembled prompt (~74 KB/row) and
    # was being loaded in bulk for two tiny things — the world-change chips and
    # the emit block replayed into history. Lift just that slice into its own
    # column so the snapshot can be deferred. Backfilled by _backfill_world_delta.
    (36, "ALTER TABLE actions ADD COLUMN world_delta JSON"),
    # Egress, part two: `variants` holds every discarded retry attempt, but a
    # list response only needs how many there are. Keep the count beside it so
    # the column itself can be deferred — otherwise each retry permanently adds
    # ~5 KB to every later load of that adventure. Backfilled by
    # _backfill_variant_count.
    (37, "ALTER TABLE actions ADD COLUMN variant_count INTEGER NOT NULL DEFAULT 0"),
    # Egress, round three: a 1536-dimension embedding written as a JSON list is
    # ~31 KB, and the whole bank is fetched every turn to rank it. Packed
    # float32 is 6 KB for the same numbers, exactly (see vectors.py). Dimensions
    # are unchanged, so this is a format conversion — no re-embedding, no API
    # calls. Backfilled by _backfill_embedding_blob. The old JSON column is left
    # in place and keeps being written until a follow-up migration drops it.
    (38, {"sqlite": "ALTER TABLE memories ADD COLUMN embedding_blob BLOB",
          "default": "ALTER TABLE memories ADD COLUMN embedding_blob BYTEA"}),
    # ...and the one-bit answer beside it, so the Memories drawer and the embed
    # queue can ask "has this got a vector?" without fetching one. Same shape as
    # actions.variant_count beside actions.variants. TRUE/FALSE and a boolean
    # DEFAULT are spelled the same on both dialects; 0/1 would not be.
    (39, "ALTER TABLE memories ADD COLUMN embedded BOOLEAN NOT NULL DEFAULT false"),
    (40, "UPDATE memories SET embedded = true WHERE embedding_blob IS NOT NULL"),
    # Memory bank capacity 200 -> 80. Only rows still on the old default move,
    # so anyone who picked a value keeps it — same rule as migration 29.
    # Adventures already over 80 evict down on their next turn.
    (41, "UPDATE settings SET memory_bank_capacity = 80 WHERE memory_bank_capacity = 200"),
    # The JSON vectors, gone. Migration 38 left them in place so a rollback
    # could still find them; production has since been verified reading from
    # embedding_blob (schema_version 41, 134/134 backfilled), so the column is
    # now 4 MB of a 99.6 MB database holding nothing anyone reads. DROP COLUMN
    # is spelled the same on both dialects — SQLite has had it since 3.35.
    (42, "ALTER TABLE memories DROP COLUMN embedding"),
]

LATEST_VERSION = max((v for v, _ in MIGRATIONS), default=1)

# Migrations that need a data pass after their DDL, keyed by version.
WORLD_DELTA_VERSION = 36
VARIANT_COUNT_VERSION = 37
EMBEDDING_BLOB_VERSION = 38

# Vectors converted per round trip. Small enough that the backfill never holds
# more than a few megabytes, large enough that it isn't a query per row.
BACKFILL_BATCH = 200


def _backfill_world_delta(conn) -> None:
    """Populate actions.world_delta from the existing context_snapshot.

    Runs entirely server-side: the snapshots are the reason this change exists,
    so pulling ~40 MB of them into Python to rewrite a slice would defeat the
    point. Dialect-specific because SQLite and Postgres spell JSON access
    differently, and both have to work (SQLite locally and in tests).
    """
    if conn.dialect.name == "sqlite":
        sql = """
            UPDATE actions SET world_delta = json_object(
                'delta',   json_extract(context_snapshot, '$.world_state.delta'),
                'applied', json_extract(context_snapshot, '$.world_state.report.applied')
            )
            WHERE world_delta IS NULL
              AND context_snapshot IS NOT NULL
              AND json_extract(context_snapshot, '$.world_state') IS NOT NULL
        """
    else:
        sql = """
            UPDATE actions SET world_delta = jsonb_build_object(
                'delta',   context_snapshot::jsonb #> '{world_state,delta}',
                'applied', context_snapshot::jsonb #> '{world_state,report,applied}'
            )
            WHERE world_delta IS NULL
              AND context_snapshot IS NOT NULL
              AND jsonb_exists(context_snapshot::jsonb, 'world_state')
        """
    conn.execute(text(sql))


def _backfill_variant_count(conn) -> None:
    """Populate actions.variant_count from the existing variants list.

    Server-side for the same reason as _backfill_world_delta: `variants` is the
    column being taken off the wire, so counting it in Python would mean
    dragging every stored attempt across the network once to avoid dragging it
    across forever.
    """
    if conn.dialect.name == "sqlite":
        sql = """
            UPDATE actions SET variant_count = json_array_length(variants)
            WHERE variants IS NOT NULL AND json_valid(variants)
        """
    else:
        sql = """
            UPDATE actions SET variant_count = jsonb_array_length(variants::jsonb)
            WHERE variants IS NOT NULL
              AND jsonb_typeof(variants::jsonb) = 'array'
        """
    conn.execute(text(sql))


def _for_dialect(sql: str | dict[str, str], dialect: str) -> str:
    return sql if isinstance(sql, str) else sql.get(dialect, sql["default"])


def _backfill_embedding_blob(conn) -> None:
    """Repack memories.embedding (JSON list) into memories.embedding_blob.

    The one backfill here that has to come through Python: struct packing has
    no portable SQL spelling, so unlike migrations 36 and 37 this pays a
    one-time read of every vector (~4 MB in production) to stop paying three
    megabytes every turn. Batched so the read is bounded whatever the bank
    grows to.

    Reads the JSON defensively — SQLite hands back the raw string while psycopg
    has already parsed it into a list — and skips anything that isn't a
    non-empty list, so one malformed row can't strand the migration.
    """
    last_id = 0
    while True:
        rows = conn.execute(
            text("""
                SELECT id, embedding FROM memories
                WHERE embedding IS NOT NULL AND embedding_blob IS NULL AND id > :last
                ORDER BY id LIMIT :batch
            """),
            {"last": last_id, "batch": BACKFILL_BATCH},
        ).all()
        if not rows:
            return
        for row_id, stored in rows:
            vector = json.loads(stored) if isinstance(stored, str) else stored
            if not isinstance(vector, list) or not vector:
                continue
            conn.execute(
                text("UPDATE memories SET embedding_blob = :blob WHERE id = :id"),
                {"blob": vectors.pack(vector), "id": row_id},
            )
        last_id = rows[-1][0]


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
                conn.execute(text(_for_dialect(sql, conn.dialect.name)))
                if version == WORLD_DELTA_VERSION:
                    _backfill_world_delta(conn)
                if version == VARIANT_COUNT_VERSION:
                    _backfill_variant_count(conn)
                if version == EMBEDDING_BLOB_VERSION:
                    _backfill_embedding_blob(conn)
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
