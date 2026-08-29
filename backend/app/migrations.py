"""Lightweight versioned schema migrations.

How it works:

* `Base.metadata.create_all()` creates a fresh database, which is always
  current, and stamps it with `LATEST_VERSION`.
* An existing database runs every migration whose version is greater than its
  stored version, in order, and is then stamped.

The version is stored in SQLite's `PRAGMA user_version`, or in a one-row
`schema_version` table on Postgres, which has no `PRAGMA`.

To change the schema, update `models.py`, which keeps fresh databases current,
and append a `(version, sql)` pair here, which upgrades existing databases. Keep
migrations idempotent where that is cheap, such as with `IF NOT EXISTS`.
Migrations up to 23 predate Postgres support and use SQLite-only syntax. That is
safe, because every Postgres database starts fresh: `create_all` creates it, the
code stamps it `LATEST`, and it never replays those migrations. Migrations added
from Phase 9 on must run on both dialects.
"""

import json
import re
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, Text,
    inspect, text,
)
from sqlalchemy.engine import Engine

from . import compression, vectors
from .database import Base

# Each entry is a version and the SQL to run when upgrading past it. Append to
# this list, and never reorder it. The SQL is a string, or a `{dialect: sql}` map
# with a `default` entry when the two dialects need different syntax, such as
# BLOB against BYTEA.
MIGRATIONS: list[tuple[int, str | dict[str, str]]] = [
    # Phase 6: auto-summarization and the memory bank. `create_all` creates the
    # `memories` table itself, and it runs for existing databases too.
    (2, "ALTER TABLE adventures ADD COLUMN auto_summarize BOOLEAN NOT NULL DEFAULT 0"),
    (3, "ALTER TABLE adventures ADD COLUMN memory_bank_enabled BOOLEAN NOT NULL DEFAULT 0"),
    (4, "ALTER TABLE adventures ADD COLUMN memory_cursor INTEGER NOT NULL DEFAULT 0"),
    (5, "ALTER TABLE adventures ADD COLUMN summary_cursor INTEGER NOT NULL DEFAULT 0"),
    (6, "ALTER TABLE settings ADD COLUMN summary_model VARCHAR(200) NOT NULL DEFAULT ''"),
    (7, "ALTER TABLE settings ADD COLUMN embedding_model VARCHAR(200) NOT NULL DEFAULT ''"),
    (8, "ALTER TABLE settings ADD COLUMN memory_bank_capacity INTEGER NOT NULL DEFAULT 200"),
    (9, "ALTER TABLE settings ADD COLUMN memory_top_k INTEGER NOT NULL DEFAULT 5"),
    # Repair duplicate action indexes. The player action and the AI action of
    # one turn used to get the same index, so renumber each adventure's actions
    # to 0..n-1 in their existing order. `UPDATE..FROM` computes the ranks as a
    # snapshot before any row is rewritten. A correlated subquery would read
    # partially updated rows and could produce duplicates again.
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
    # Reasoning-model support: a separate thinking budget and stored reasoning
    # text.
    (11, "ALTER TABLE settings ADD COLUMN reasoning_max_tokens INTEGER NOT NULL DEFAULT 0"),
    (12, "ALTER TABLE actions ADD COLUMN reasoning TEXT"),
    # Phase 8: optional accounts. `create_all` creates the `users` table itself.
    # These migrations move every pre-existing row under a local user with
    # `id=1`, so a single-user install keeps working unchanged.
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
    # Link each adventure-script copy back to its library Script, so a player
    # can re-sync it on demand. The column is NULL for a copy made before it
    # existed.
    (24, "ALTER TABLE adventure_scripts ADD COLUMN source_script_id INTEGER "
         "REFERENCES scripts(id) ON DELETE SET NULL"),
    # A per-action snapshot of the shared `script_state` as it was before that
    # action's hooks ran, so undo and retry can roll the state back. JSON is
    # valid on both SQLite and Postgres.
    (25, "ALTER TABLE actions ADD COLUMN state_before JSON"),
    # Phase 12: RPG world state. `stat_schema` defines a scenario's stats,
    # bands, rules, and milestones. `world_state` holds an adventure's live
    # values. `world_state_before` snapshots those values per action for undo and
    # retry, the same way `state_before` does. JSON is valid on both SQLite and
    # Postgres.
    (26, "ALTER TABLE scenarios ADD COLUMN stat_schema JSON"),
    (27, "ALTER TABLE adventures ADD COLUMN world_state JSON"),
    (28, "ALTER TABLE actions ADD COLUMN world_state_before JSON"),
    # Raise the default context budget from 4096 to 16384, because Phase 12 adds
    # a stat guide and the world state to every turn. This updates only rows
    # still on the old default, so anyone who chose a custom value keeps it.
    (29, "UPDATE settings SET context_token_budget = 16384 WHERE context_token_budget = 4096"),
    # Scenario cover art, either an external URL or an inline base64 data URI.
    # The column is TEXT rather than VARCHAR, because a downscaled data URI runs
    # to tens of kilobytes.
    (30, "ALTER TABLE scenarios ADD COLUMN image TEXT NOT NULL DEFAULT ''"),
    # The emoji or glyph fallback, used when `image` is empty.
    (31, "ALTER TABLE scenarios ADD COLUMN icon VARCHAR(16) NOT NULL DEFAULT ''"),
    # The `${Placeholder}` answers given when the adventure started. They are
    # stored so that "Update from scenario" can fill the copied text again. The
    # column is NULL for an adventure created before it existed, and that
    # adventure prompts for the answers again on its first refresh.
    (32, "ALTER TABLE adventures ADD COLUMN placeholders JSON"),
    # Which part of the scenario a copied story card came from, either
    # `card:<id>` or `npc:<key>`, so that a refresh updates and removes only the
    # scenario-derived cards and leaves player-authored ones unchanged. NULL
    # means the card is player-authored, or that the copy predates this column.
    # A predating copy is matched by name once and then tagged.
    (33, "ALTER TABLE story_cards ADD COLUMN source_ref VARCHAR(64)"),
    # Retry history: every attempt made for an AI turn, oldest first, so that a
    # retry appends rather than deletes. NULL means the turn was never retried
    # and the row is its only version, which is also the correct reading for
    # every action that predates this column.
    (34, "ALTER TABLE actions ADD COLUMN variants JSON"),
    (35, "ALTER TABLE actions ADD COLUMN variant_index INTEGER NOT NULL DEFAULT 0"),
    # Egress: `context_snapshot` holds the whole assembled prompt, about 74 kB
    # per row, and bulk reads loaded it for two small values: the world-change
    # chips and the emit block replayed into history. Move that slice into its
    # own column so the snapshot can be deferred. `_backfill_world_delta` fills
    # the new column.
    (36, "ALTER TABLE actions ADD COLUMN world_delta JSON"),
    # Egress, part two: `variants` holds every discarded retry attempt, but a
    # list response needs only how many there are. Store the count next to it so
    # the column can be deferred. Otherwise each retry adds about 5 kB to every
    # later load of that adventure. `_backfill_variant_count` fills the new
    # column.
    (37, "ALTER TABLE actions ADD COLUMN variant_count INTEGER NOT NULL DEFAULT 0"),
    # Egress, part three: a 1536-dimension embedding written as a JSON list is
    # about 31 kB, and ranking fetches the whole bank every turn. Packed float32
    # holds the same numbers exactly in 6 kB. See `vectors.py`. The dimensions do
    # not change, so this is a format conversion with no re-embedding and no API
    # calls. `_backfill_embedding_blob` fills the new column. The old JSON column
    # stays in place and is still written until a later migration drops it.
    (38, {"sqlite": "ALTER TABLE memories ADD COLUMN embedding_blob BLOB",
          "default": "ALTER TABLE memories ADD COLUMN embedding_blob BYTEA"}),
    # A one-bit flag next to the vector, so the Memories drawer and the embed
    # queue can test whether a memory has a vector without fetching one. This
    # matches `actions.variant_count` next to `actions.variants`. TRUE, FALSE,
    # and a boolean DEFAULT use the same syntax on both dialects, and 0 and 1
    # would not.
    (39, "ALTER TABLE memories ADD COLUMN embedded BOOLEAN NOT NULL DEFAULT false"),
    (40, "UPDATE memories SET embedded = true WHERE embedding_blob IS NOT NULL"),
    # Memory bank capacity from 200 to 80. Only rows still on the old default
    # change, so anyone who chose a value keeps it. Migration 29 follows the same
    # rule. An adventure already over 80 evicts down on its next turn.
    (41, "UPDATE settings SET memory_bank_capacity = 80 WHERE memory_bank_capacity = 200"),
    # Drop the JSON vectors. Migration 38 left them in place so a rollback could
    # still find them. Production has since been verified reading from
    # `embedding_blob`, at schema version 41 with 134 of 134 rows backfilled, so
    # the column is 4 MB of a 99.6 MB database that nothing reads. DROP COLUMN
    # uses the same syntax on both dialects, and SQLite has supported it since
    # 3.35.
    (42, "ALTER TABLE memories DROP COLUMN embedding"),
    # Compress `context_snapshot`. One column holds 89% of the database. It
    # stores assembled prompts that no query filters on and that one screen
    # reads, one row at a time. Postgres already TOASTs the column, but pglz
    # reaches only 1.7x, while zlib reaches three to four times on the same text.
    # Deferring the column already fixed the read cost. This migration is about
    # the 512 MB the free tier allows.
    #
    # This takes three steps, because a column cannot portably change type in
    # place. Add the new column, convert into it with
    # `_backfill_context_snapshot`, which verifies that every row round-trips
    # before the old column is dropped, then swap the names so the model still
    # calls it `context_snapshot`.
    #
    # Postgres does not release the disk space on its own. DROP COLUMN only
    # marks the column dropped, and the backfill's UPDATE leaves one dead tuple
    # per row, so the table grows before it shrinks. The peak is roughly twice
    # the starting size while both columns exist. Autovacuum makes that space
    # reusable but does not shrink the files. Run this once after the deploy
    # that ships this migration:
    #
    #     VACUUM FULL actions;
    #
    # That command needs exclusive access and free space equal to the finished
    # table. On the 2026-08-17 figures, the database is 99.6 MB, peaks near 200
    # MB, and settles at about 53 MB once vacuumed, against a 512 MB tier.
    # Skipping the vacuum is safe and leaves the saving unrealized.
    (43, {"sqlite": "ALTER TABLE actions ADD COLUMN context_snapshot_z BLOB",
          "default": "ALTER TABLE actions ADD COLUMN context_snapshot_z BYTEA"}),
    (44, "ALTER TABLE actions DROP COLUMN context_snapshot"),
    (45, "ALTER TABLE actions RENAME COLUMN context_snapshot_z TO context_snapshot"),
    # Phase 14, SP1: the story becomes a tree. Every action gains the branch it
    # was played on and its depth along that branch, adventures gain a head
    # pointer, and memories attach to the node that produced them. `create_all`
    # creates the `branches` table itself, as it did for `memories`.
    #
    # Nothing reads these columns yet. SP2 moves the reads onto them. This
    # subphase exists so that every row already has the columns by the time
    # anything reads them, including the rows written between the two deploys,
    # which `app/tree.py` stamps. The legacy `index`, `variants`,
    # `variant_index`, and `variant_count` columns stay in place, unread, until
    # the tree is proven live.
    #
    # This rewrites every row of `actions` twice, once per ADD COLUMN backfill
    # pass on Postgres, so run this once after the deploy that ships it:
    #
    #     VACUUM FULL actions;
    #
    # Run it on the direct endpoint, not on -pooler. This is the 144 MB result
    # from 2026-08-17: a rewrite roughly doubles the table, and only a VACUUM
    # FULL releases the space. Skipping it is safe and leaves the table large.
    (46, "ALTER TABLE actions ADD COLUMN branch_id INTEGER "
         "REFERENCES branches(id) ON DELETE CASCADE"),
    (47, "ALTER TABLE actions ADD COLUMN depth INTEGER"),
    # `head_branch_id` has no REFERENCES clause. `branches.adventure_id` already
    # points the other way, and a second constraint would make the pair a cycle
    # that `create_all` cannot order. See the column comment in `models.py`.
    (48, "ALTER TABLE adventures ADD COLUMN head_branch_id INTEGER"),
    (49, "ALTER TABLE adventures ADD COLUMN head_depth INTEGER NOT NULL DEFAULT -1"),
    (50, "ALTER TABLE memories ADD COLUMN branch_id INTEGER "
         "REFERENCES branches(id) ON DELETE CASCADE"),
    (51, "ALTER TABLE memories ADD COLUMN depth INTEGER"),
    # The index every branch clause needs, plus the data pass that fills the six
    # columns above. `_backfill_tree` runs at this version because it needs all
    # six columns to exist.
    (52, "CREATE INDEX IF NOT EXISTS ix_actions_branch_depth ON actions (branch_id, depth)"),
    # Phase 14, SP3: the memory cursor and the summary cursor stop being
    # positions in the story and become nodes in it, as the `(branch, depth)` of
    # the last action each pass covered. The legacy `memory_cursor` and
    # `summary_cursor` columns stay, unread, until SP8 drops them along with
    # `actions.index`.
    #
    # Unlike migrations 46 to 52, this rewrites `adventures` rather than
    # `actions`, which is a few hundred rows against a few hundred thousand, so
    # it needs no VACUUM FULL of its own. The one SP1's deploy asks for is still
    # outstanding.
    (53, "ALTER TABLE adventures ADD COLUMN memory_cursor_branch_id INTEGER"),
    (54, "ALTER TABLE adventures ADD COLUMN memory_cursor_depth INTEGER NOT NULL DEFAULT -1"),
    (55, "ALTER TABLE adventures ADD COLUMN summary_cursor_branch_id INTEGER"),
    (56, "ALTER TABLE adventures ADD COLUMN summary_cursor_depth INTEGER NOT NULL DEFAULT -1"),
    # Phase 14, SP4: a retry stops rewriting a row and writes a sibling next to
    # it. `live` records which sibling the story tells. `state_after` and
    # `world_state_after` give each attempt its own outcome to switch back to.
    # The legacy `variants`, `variant_index`, `state_before`, and
    # `world_state_before` columns stay, unread, until SP8.
    #
    # This rewrites every row of `actions` three times, once per ADD COLUMN
    # backfill pass on Postgres, and then inserts one row per discarded attempt,
    # so run this once after the deploy that ships it:
    #
    #     VACUUM FULL actions;
    #
    # Run it on the direct endpoint, not on -pooler. This is the same 144 MB
    # result as SP1.
    (57, "ALTER TABLE actions ADD COLUMN live BOOLEAN NOT NULL DEFAULT true"),
    (58, "ALTER TABLE actions ADD COLUMN state_after JSON"),
    (59, "ALTER TABLE actions ADD COLUMN world_state_after JSON"),
    # The two data passes get a version of their own, so that they run after all
    # three columns exist. `_backfill_state_after` derives the after-snapshots
    # from the before-snapshots, and `_split_variants_into_siblings` splits each
    # `variants` list into sibling rows. The statement below is the index those
    # siblings would make worth having, but a group lookup is `(branch_id,
    # depth)` over a few rows and `ix_actions_branch_depth` already serves it, so
    # the statement does nothing and only gives the passes a version to run
    # at.
    (60, "CREATE INDEX IF NOT EXISTS ix_actions_branch_depth ON actions (branch_id, depth)"),
    # Phase 14, SP7: a branch can be named. NULL means nobody named the branch,
    # which is true of every branch that exists when this runs, so there is no
    # backfill and nothing to derive. `branches` holds a few rows per adventure
    # rather than one per turn, so unlike SP1 and SP4 this rewrite covers a few
    # hundred rows against a few hundred thousand and needs no VACUUM FULL of
    # its own.
    (61, "ALTER TABLE branches ADD COLUMN name VARCHAR(80)"),
    # Phase 14, SP7: every memory gets a node. A hand-written memory used to
    # keep a NULL depth, which no fork could bound, so it followed the reader
    # onto branches whose story it never described. A new memory anchors at the
    # head. An existing memory lands at depth 0 of the branch it is on, which is
    # the only choice that removes a memory from no path: 0 is at or before every
    # fork point, so a memory stays visible from the same paths it is visible
    # from today. Anchoring an existing memory at the tip would have removed it
    # from every branch forked earlier than the memory was written.
    (62, "UPDATE memories SET depth = 0 WHERE depth IS NULL"),
    # Phase 14, SP9: the node a node was played after, so a turn's attempts can
    # be grouped by parent rather than by coordinate. A coordinate stops
    # identifying which attempts belong together as soon as one of them is forked
    # onto its own branch, because it leaves its siblings behind and reads 1/1
    # next to their 1/3.
    #
    # The column is nullable, and the backfill leaves it NULL wherever it cannot
    # place a row correctly. See `_backfill_parents`. `attempts.group` falls back
    # to the coordinate for those rows, which is the rule they were written
    # under.
    (63, "ALTER TABLE actions ADD COLUMN parent_id INTEGER REFERENCES actions(id) ON DELETE SET NULL"),
    (64, "CREATE INDEX IF NOT EXISTS ix_actions_parent ON actions (parent_id)"),
    # Phase 17: `Settings.stream` was dead state. Nothing ever read it, and every
    # turn streams. This is item S1 in `docs/self-review.md`. The table holds one
    # row per user, so the rewrite is small and needs no VACUUM FULL.
    (65, "ALTER TABLE settings DROP COLUMN stream"),
    # Phase 17, SP8: drop the eight columns the story tree replaced. Each one was
    # kept past the migration that superseded it so that a rollback found a real
    # value on the rows the newer build wrote. The tree has run in production
    # since Phase 14, so the rollback window is closed.
    #
    # `actions.index` was the global turn number. `depth` replaced it, and the
    # last reader, the number issued to a new row, went with this migration.
    # `variants` held the retry history as a repeating group, and `variant_index`
    # and `variant_count` described that group's shape. Each attempt is its own
    # row now, ordered by `id`. `state_before` and `world_state_before` recorded
    # the state a node started from, which every attempt at a turn shares; the
    # "after" columns record each attempt's own outcome instead.
    # `adventures.memory_cursor` and `summary_cursor` were positions into a flat
    # list, and the branch and depth pairs beside them replaced both.
    #
    # Dropping a column rewrites the toasted values on Postgres and nothing
    # reclaims that space on its own. Run `VACUUM FULL actions;` on the direct
    # Neon endpoint after the deploy, not the pooled one. It takes an ACCESS
    # EXCLUSIVE lock, so the app blocks on `actions` while it runs.
    (66, "ALTER TABLE actions DROP COLUMN variants"),
    (67, "ALTER TABLE actions DROP COLUMN variant_index"),
    (68, "ALTER TABLE actions DROP COLUMN variant_count"),
    (69, "ALTER TABLE actions DROP COLUMN state_before"),
    (70, "ALTER TABLE actions DROP COLUMN world_state_before"),
    # `index` is a keyword in SQLite, so the column name is quoted.
    (71, 'ALTER TABLE actions DROP COLUMN "index"'),
    (72, "ALTER TABLE adventures DROP COLUMN memory_cursor"),
    (73, "ALTER TABLE adventures DROP COLUMN summary_cursor"),
]

LATEST_VERSION = max((v for v, _ in MIGRATIONS), default=1)

# Migrations that need a data pass after their DDL, keyed by version.
WORLD_DELTA_VERSION = 36
VARIANT_COUNT_VERSION = 37
EMBEDDING_BLOB_VERSION = 38
SNAPSHOT_COMPRESS_VERSION = 43
TREE_BACKFILL_VERSION = 52
CURSOR_ANCHOR_VERSION = 56
SIBLING_SPLIT_VERSION = 60
PARENT_BACKFILL_VERSION = 64

# An adventure with no actions has no tip. A value of -1 keeps the rule that the
# next node goes at `head_depth + 1` true without a special case. This matches
# `tree.NO_DEPTH`.
NO_DEPTH = -1

# How many snapshots are converted per round trip. This is much smaller than
# `BACKFILL_BATCH`, because a vector is 6 kB and a snapshot is 232 kB, so 200
# snapshots would hold 46 MB at once.
SNAPSHOT_BATCH = 50

# How many vectors are converted per round trip. The value is small enough that
# the backfill never holds more than a few megabytes, and large enough that it
# does not run one query per row.
BACKFILL_BATCH = 200


def _backfill_world_delta(conn) -> None:
    """Populates `actions.world_delta` from the existing `context_snapshot`.

    The pass runs entirely on the server. The snapshots are the reason this
    change exists, so reading about 40 MB of them into Python to rewrite one
    slice would defeat its purpose. The SQL is dialect-specific, because SQLite
    and Postgres use different JSON syntax and both have to work. SQLite runs
    locally and in the tests.
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


def _backfill_parents(conn) -> None:
    """Points every node at the attempt it was played after.

    The pass makes one query, which reads the live node one depth back on the
    same branch. That covers a linear story, and every adventure written before
    SP9 is linear. Forking reached the screen in SP7, and the tree was found
    unusable before anyone forked with it.

    The rows left NULL are the first node of a forked branch, whose parent is on
    an ancestor branch and cannot be found without walking `lineage` for each
    row. `attempts.group` falls back to the coordinate for a NULL parent, which
    is the rule those rows were written under, so the fallback returns the
    original answer for them rather than a worse one. Every fork made from SP9 on
    sets `parent_id` at write time and does not use this pass.

    The query correlates to the row being updated rather than numbering the
    table, so the planner uses `ix_actions_branch_depth`. This follows from
    `_backfill_cursor_anchors`, which numbered every action once per adventure.
    """
    conn.execute(
        text(
            """
            UPDATE actions SET parent_id = (
                SELECT prev.id FROM actions AS prev
                WHERE prev.adventure_id = actions.adventure_id
                  AND prev.branch_id = actions.branch_id
                  AND prev.depth = actions.depth - 1
                  AND prev.live = TRUE
                ORDER BY prev.id
                LIMIT 1
            )
            WHERE parent_id IS NULL
              AND branch_id IS NOT NULL
              AND depth IS NOT NULL
            """
        )
    )


def _backfill_variant_count(conn) -> None:
    """Populates `actions.variant_count` from the existing `variants` list.

    The pass runs on the server for the same reason as `_backfill_world_delta`.
    `variants` is the column this change removes from responses, so counting it
    in Python would read every stored attempt over the network once in order to
    stop reading it on every request.
    """
    if not _has_columns(conn, "actions", "variants", "variant_count"):
        return
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


# Matches the ADD COLUMN migrations in this file. Every one is written above,
# so this pattern parses only SQL this file controls.
_ADD_COLUMN = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+\"?(\w+)\"?", re.I)
_DROP_COLUMN = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+\"?(\w+)\"?", re.I)


def _has_columns(conn, table: str, *columns: str) -> bool:
    """Returns `True` when `table` has every one of `columns`.

    The data passes below read columns that later migrations drop. A pass only
    ever has real work to do on a database old enough to still carry them, and
    a `create_all` database is already current, so the guard skips the pass
    rather than failing on a column that is not there. This is the same rule
    `_column_already_there` applies to DDL, written for the passes.
    """
    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        return False
    present = {col["name"] for col in inspector.get_columns(table)}
    return all(column in present for column in columns)


def _column_already_gone(conn, sql: str) -> bool:
    """Returns `True` when `sql` drops a column the table no longer has.

    This is the counterpart to `_column_already_there`, for the same reason.
    `create_all` builds the current schema, which is already missing every
    column a migration drops. Replaying from an older stamp against a database
    built that way would fail on a column that is not there.
    """
    match = _DROP_COLUMN.match(sql)
    if match is None:
        return False
    table, column = match.group(1), match.group(2)
    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        return False
    return column not in {col["name"] for col in inspector.get_columns(table)}


def _column_already_there(conn, sql: str) -> bool:
    """Returns `True` when `sql` adds a column the table already has.

    This is the `IF NOT EXISTS` the module docstring asks for, written in Python
    because SQLite has no syntax for it on ADD COLUMN. Without this check, a
    database that carries a newer column than its stamp claims fails on a
    duplicate column and never runs the backfill. That database is common:
    `create_all` always builds the current schema, so it is what every test that
    replays a migration starts from, and SQLite cannot drop those columns again
    once a foreign key names them.
    """
    match = _ADD_COLUMN.match(sql)
    if match is None:
        return False
    table, column = match.group(1), match.group(2)
    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        return False
    return column in {col["name"] for col in inspector.get_columns(table)}


def _backfill_embedding_blob(conn) -> None:
    """Repacks `memories.embedding`, a JSON list, into `memories.embedding_blob`.

    This is the only backfill here that runs through Python, because struct
    packing has no portable SQL form. Unlike migrations 36 and 37, it pays a
    one-time read of every vector, about 4 MB in production, to stop reading
    three megabytes every turn. It runs in batches, so the read stays bounded
    however large the bank grows.

    The pass reads the JSON defensively, because SQLite returns the raw string
    while psycopg has already parsed it into a list. It skips any value that is
    not a non-empty list, so one malformed row cannot block the migration.
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


def _backfill_context_snapshot(conn) -> None:
    """Compresses `actions.context_snapshot` into `actions.context_snapshot_z`.

    This runs between migrations 43 and 44, which is the only point where both
    columns exist. Migration 44 drops the original column, so unlike every other
    backfill here this one destroys data if it is wrong, and every row it
    converts belongs to somebody's game. Each row is therefore decompressed again
    and compared with the input before it counts as converted. A row that fails
    to round-trip aborts the run rather than being skipped: the transaction rolls
    back, the DROP never runs, and the prompts are still there for another
    attempt.

    The pass reads the JSON as defensively as the vector backfill does, because
    SQLite returns a raw string and psycopg has already parsed it.
    """
    last_id = 0
    while True:
        rows = conn.execute(
            text("""
                SELECT id, context_snapshot FROM actions
                WHERE context_snapshot IS NOT NULL
                  AND context_snapshot_z IS NULL AND id > :last
                ORDER BY id LIMIT :batch
            """),
            {"last": last_id, "batch": SNAPSHOT_BATCH},
        ).all()
        if not rows:
            return
        params = []
        for row_id, stored in rows:
            value = json.loads(stored) if isinstance(stored, str) else stored
            if value is None:
                continue
            packed = compression.pack(value)
            if compression.unpack(packed) != value:
                raise RuntimeError(
                    f"context_snapshot for action {row_id} did not survive a "
                    "compress/decompress round trip; refusing to drop the "
                    "original column"
                )
            params.append({"z": packed, "id": row_id})
        if params:
            # Run one executemany per batch rather than one statement per row.
            # This runs at container start, before the port opens, against a
            # database across a network. A thousand round trips is the
            # difference between a deploy that starts and a health check that
            # times out waiting for it.
            conn.execute(
                text("UPDATE actions SET context_snapshot_z = :z WHERE id = :id"),
                params,
            )
        last_id = rows[-1][0]


# Returns SQL for the root branch of the adventure a row belongs to. It uses
# MIN(id) rather than a LIMIT, so the result is a plain scalar subquery on both
# dialects and stays deterministic if a database ends up with two roots for one
# adventure.
def _root_branch_of(column: str) -> str:
    return (
        "(SELECT MIN(b.id) FROM branches b "
        f"WHERE b.adventure_id = {column} AND b.parent_branch_id IS NULL)"
    )


def _backfill_tree(conn) -> None:
    """Rewrites every existing adventure's linear story as a tree with one branch.

    The pass creates one root branch per adventure, sets `depth` to the old
    `index`, points the head at the tip, and attaches every memory to the node it
    summarized. It copies no row, deletes no row, and changes no ordering.
    `index` and `depth` hold the same numbers when it finishes, which makes the
    claim that current adventures are unaffected testable.

    The pass runs on the server. `actions` is the table that fills the disk, and
    reading it into Python to write two integers per row would repeat a mistake
    this project has already made twice. Each statement is guarded on its own
    target being unset, so a run that fails partway through resumes rather than
    applying twice.
    """
    if not _has_columns(conn, "actions", "index"):
        return
    sqlite = conn.dialect.name == "sqlite"

    # 1. A root branch per adventure. Its lineage names the row's own id, which
    #    does not exist until the row does, so lineage starts as an empty list.
    #    `create_all` creates `branches` with lineage NOT NULL, so an empty array
    #    is how an unfilled lineage has to be stored.
    conn.execute(text("""
        INSERT INTO branches (adventure_id, parent_branch_id, fork_depth, lineage, created_at)
        SELECT a.id, NULL, NULL, '[]', CURRENT_TIMESTAMP
        FROM adventures a
        WHERE NOT EXISTS (SELECT 1 FROM branches b WHERE b.adventure_id = a.id)
    """))

    # 2. Set lineage to `[[own_id, null]]`, which is one uncapped entry,
    #    because the root branch is the whole story. The database's own JSON
    #    functions build it, because binding a JSON string as a parameter has no
    #    form that means the same thing to SQLite, which sees TEXT, and to
    #    Postgres, which sees json. The guard tests a length rather than
    #    `= '[]'`, because Postgres `json` has no equality operator.
    conn.execute(text(
        "UPDATE branches SET lineage = json_array(json_array(id, null)) "
        "WHERE json_array_length(lineage) = 0"
        if sqlite else
        "UPDATE branches SET lineage = "
        "jsonb_build_array(jsonb_build_array(id, null))::json "
        "WHERE json_array_length(lineage) = 0"
    ))

    # 3. Move every action onto that branch, at the depth its index implies.
    #    Deleting an action in the middle left gaps in `index`, and those gaps
    #    carry over on purpose. Depth has to preserve the order the story is read
    #    in, and renumbering here would move every cursor that points past a
    #    gap.
    conn.execute(text(f"""
        UPDATE actions
        SET branch_id = {_root_branch_of('actions.adventure_id')},
            depth = "index"
        WHERE branch_id IS NULL
    """))

    # 4. Set the head to the root branch and to the depth of its newest node.
    conn.execute(text(f"""
        UPDATE adventures
        SET head_branch_id = {_root_branch_of('adventures.id')},
            head_depth = COALESCE(
                (SELECT MAX(a."index") FROM actions a WHERE a.adventure_id = adventures.id),
                {NO_DEPTH}
            )
        WHERE head_branch_id IS NULL
    """))

    # 5. Attach memories to the node that produced them. `source_end` is the
    #    index of the last action a memory summarized, so it is that node's
    #    depth. A hand-written memory has no node and keeps a NULL depth.
    conn.execute(text(f"""
        UPDATE memories
        SET branch_id = {_root_branch_of('memories.adventure_id')},
            depth = source_end
        WHERE branch_id IS NULL
    """))


# A frozen copy of `context.history._STORY_TEXT` as it stood at version 56,
# which selects text that is not blank once whitespace is stripped. It is written
# out here rather than imported, because a migration has to keep meaning what it
# meant on the day it ran, while the module can change. `char()` is `chr()` on
# Postgres, and there is no third form, so the SQL depends on the dialect.
def _story_text_sql(column: str, sqlite: bool) -> str:
    char = "char" if sqlite else "chr"
    folded = column
    for code in (10, 13, 9):  # newline, carriage return, tab
        folded = f"replace({folded}, {char}({code}), ' ')"
    return f"trim({folded}) <> ''"


def _backfill_cursor_anchors(conn) -> None:
    """Converts each adventure's two cursors from positions into nodes.

    A `memory_cursor` of 12 meant that the first twelve story actions were
    covered. The twelfth story action, in depth order, is the node that means the
    same thing and keeps meaning it after an action before it is deleted. The
    translation is therefore a `ROW_NUMBER()` over the story plus a lookup at the
    cursor's own value.

    The arithmetic has to handle two cases:

    * A cursor past the end of the story. This is legitimate. An adventure that
      was caught up under the older rule can have a cursor equal to its action
      count, and `run_post_turn` clamped it on every pass. No `rn` matches, so
      the query falls back to the deepest node that exists, which still means
      caught up.
    * A cursor of 0, which is what most adventures have. Nothing is covered, the
      column default already records that, and no row is updated.

    The statement is guarded on `_depth = -1`, so that a run which fails partway
    through resumes. Every adventure it has already converted is skipped, and one
    it has not converted looks the same as an untouched row.

    The row numbering is correlated rather than ranked and then filtered.
    Numbering every action in the table and selecting one row from the result
    reads all of `actions` once per adventure, because the window function stops
    the correlation from being pushed down and the planner cannot make it
    cheaper. This runs at boot, inside the single transaction that holds the
    schema, against a database with real stories in it. Restricting the scan to
    the adventure being updated makes each pass an index lookup on
    `actions.adventure_id`, and `PARTITION BY` then partitions one adventure. The
    two forms return the same answer, because the rows the partition would
    separate are the rows the filter removes.
    """
    if not _has_columns(conn, "adventures", "memory_cursor", "summary_cursor"):
        return
    sqlite = conn.dialect.name == "sqlite"
    story = _story_text_sql("text", sqlite)
    for name in ("memory", "summary"):
        conn.execute(text(f"""
            UPDATE adventures
            SET {name}_cursor_branch_id = {_root_branch_of('adventures.id')},
                {name}_cursor_depth = COALESCE(
                    (SELECT ranked.depth FROM (
                        SELECT depth, ROW_NUMBER() OVER (
                            ORDER BY depth, id
                        ) AS rn
                        FROM actions
                        WHERE {story}
                          AND actions.adventure_id = adventures.id
                     ) AS ranked
                     WHERE ranked.rn = adventures.{name}_cursor),
                    (SELECT MAX(a.depth) FROM actions a
                      WHERE a.adventure_id = adventures.id
                        AND {_story_text_sql('a.text', sqlite)}),
                    {NO_DEPTH})
            WHERE adventures.{name}_cursor > 0
              AND adventures.{name}_cursor_depth = {NO_DEPTH}
        """))


# The per-attempt slices of a context snapshot, frozen here as they stood at
# version 60, where they were `adventures.VARIANT_SNAPSHOT_KEYS` and are now
# `attempts.ATTEMPT_KEYS`. Everything else in a snapshot is the assembled prompt,
# which every attempt at one turn shares, so a discarded attempt's row carries
# only these keys.
_ATTEMPT_KEYS = ("world_state", "script", "raw_output")


def _backfill_state_after(conn) -> None:
    """Derives the "after" snapshots SP4 reads from each action's "before" ones.

    The state a node left behind is the state the node after it started from, so
    `state_after` of action n is exactly `state_before` of action n + 1. The
    hooks that ran between them belong to this turn, and both snapshots were
    taken with those hooks already applied. The newest action of an adventure has
    no action after it, and what it left behind is what the adventure currently
    holds.

    Each column takes two statements rather than one COALESCE, because the second
    statement is guarded on there being no later action at all. A row whose
    successor predates `state_before` has to stay NULL rather than inherit the
    tip's state.

    The queries order by `index`, which is still the story's order at version 60.
    SP4 is the first migration where `depth` and `index` can disagree, and it has
    not run when this pass does.
    """
    if not _has_columns(conn, "actions", "state_before", "world_state_before"):
        return
    for column, live in (
        ("state_after", "script_state"),
        ("world_state_after", "world_state"),
    ):
        before = column.replace("_after", "_before")
        conn.execute(text(f"""
            UPDATE actions SET {column} = (
                SELECT n.{before} FROM actions n
                WHERE n.adventure_id = actions.adventure_id
                  AND n."index" > actions."index"
                ORDER BY n."index", n.id LIMIT 1
            )
            WHERE actions.{column} IS NULL
        """))
        conn.execute(text(f"""
            UPDATE actions SET {column} = (
                SELECT adv.{live} FROM adventures adv
                WHERE adv.id = actions.adventure_id
            )
            WHERE actions.{column} IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM actions n
                  WHERE n.adventure_id = actions.adventure_id
                    AND n."index" > actions."index"
              )
        """))


# The `actions` table as migration 60 finds it, declared here rather than read
# from `Base.metadata`. The split pass writes JSON values, and only a column
# type knows how to render a dict on this dialect, so it needs a Table. Reading
# the live one would tie a migration to the current model: migration 66 drops
# five of these columns, and the pass would then fail to compile on a database
# that still has them. This declaration is a snapshot of a past schema and must
# not be updated to track `models.py`.
#
# It carries its own `MetaData`, so `create_all` never sees it.
_ACTIONS_AT_60 = Table(
    "actions", MetaData(),
    Column("id", Integer, primary_key=True),
    Column("adventure_id", Integer),
    Column("index", Integer),
    Column("branch_id", Integer),
    Column("depth", Integer),
    Column("live", Boolean),
    Column("type", String(20)),
    Column("text", Text),
    Column("reasoning", Text),
    Column("context_snapshot", compression.CompressedJSON),
    Column("world_delta", JSON),
    Column("state_before", JSON),
    Column("world_state_before", JSON),
    Column("state_after", JSON),
    Column("world_state_after", JSON),
    Column("variants", JSON),
    Column("variant_count", Integer),
    Column("variant_index", Integer),
    Column("created_at", DateTime),
)


def _split_variants_into_siblings(conn) -> None:
    """Gives every discarded retry attempt its own row.

    `actions.variants` is a JSON repeating group holding the attempts at one
    turn, oldest first, with `variant_index` naming the one the row's own `text`
    duplicates. SP4 makes each attempt a node on the same branch at the same
    depth, with `live` set on exactly one of them, so this pass reads the list one
    last time and writes it out as siblings.

    The existing row keeps its own attempt and its context snapshot. That
    snapshot is the assembled prompt for the whole turn and is stored once per
    turn, never once per attempt. A sibling's snapshot carries only the slices
    that differ between attempts, which is what the JSON entry held, so this pass
    changes how the bytes are arranged rather than how many there are.

    The pass runs in Python rather than SQL, because the work iterates a JSON
    array and inserts one row per element, which the two dialects express
    differently and neither expresses well. Its cost is bounded by the number of
    turns anyone has retried rather than by the size of the table, and it reads no
    `context_snapshot`.

    The pass is resumable. A group that already has as many rows as its
    `variant_count` claims has been split, so it is skipped.
    """
    if not _has_columns(conn, "actions", "variants", "variant_index"):
        return
    actions = _ACTIONS_AT_60
    last_id = 0
    while True:
        rows = conn.execute(
            text("""
                SELECT id, adventure_id, branch_id, depth, "index", type,
                       created_at, variants, variant_index
                FROM actions
                WHERE variants IS NOT NULL AND id > :last
                ORDER BY id LIMIT :batch
            """),
            {"last": last_id, "batch": BACKFILL_BATCH},
        ).mappings().all()
        if not rows:
            return
        last_id = rows[-1]["id"]
        for row in rows:
            entries = row["variants"]
            if isinstance(entries, str):
                entries = json.loads(entries)
            if not isinstance(entries, list) or len(entries) < 2:
                continue
            _split_one_action(conn, actions, row, entries)


def _split_one_action(conn, actions, row, entries: list) -> None:
    live_index = row["variant_index"] or 0
    live_index = min(max(live_index, 0), len(entries) - 1)
    already = conn.execute(
        text("""
            SELECT COUNT(*) FROM actions
            WHERE adventure_id = :adv AND branch_id = :branch AND depth = :depth
        """),
        {"adv": row["adventure_id"], "branch": row["branch_id"], "depth": row["depth"]},
    ).scalar()
    if already and already >= len(entries):
        return  # A previous run of this pass already split it.
    # Prefer the live attempt's own recorded outcome over the one
    # `_backfill_state_after` derived from the turn after it. The two agree, but
    # only the recorded one is a fact about this attempt. If the entry has no
    # outcome to give, which happens when an adventure has no RPG layer and
    # stores no world state per attempt, leave the derived value in place rather
    # than overwrite it with NULL.
    kept = {"live": True, "variant_count": len(entries), "variant_index": live_index}
    live_state = _entry_script_state(entries[live_index])
    live_world = _entry_world_state(entries[live_index])
    if live_state is not None:
        kept["state_after"] = live_state
    if live_world is not None:
        kept["world_state_after"] = live_world
    # Use the Table rather than `text()`, here and below. These values are dicts
    # bound for JSON columns, and the column type is the only thing that knows
    # how to render one on this dialect. The table is `_ACTIONS_AT_60`, frozen
    # at the schema this pass runs against.
    conn.execute(actions.update().where(actions.c.id == row["id"]).values(**kept))
    siblings = [
        {
            "adventure_id": row["adventure_id"],
            "index": row["index"],
            "branch_id": row["branch_id"],
            "depth": row["depth"],
            "live": False,
            "type": row["type"],
            "text": str(entry.get("text") or ""),
            "reasoning": entry.get("reasoning"),
            "context_snapshot": _attempt_snapshot(entry) or None,
            "world_delta": _entry_world_delta(entry),
            "state_before": None,
            "world_state_before": None,
            "state_after": _entry_script_state(entry),
            "world_state_after": _entry_world_state(entry),
            "variants": None,
            "variant_count": len(entries),
            "variant_index": i,
            "created_at": _entry_created_at(entry, row["created_at"]),
        }
        for i, entry in enumerate(entries)
        if i != live_index and isinstance(entry, dict)
    ]
    if siblings:
        conn.execute(actions.insert(), siblings)


def _entry_script_state(entry) -> dict | None:
    state = (entry or {}).get("script_state")
    return state if isinstance(state, dict) else None


def _entry_world_state(entry) -> dict | None:
    state = ((entry or {}).get("world_state") or {}).get("state")
    return state if isinstance(state, dict) else None


def _entry_world_delta(entry) -> dict | None:
    ws = (entry or {}).get("world_state")
    if not isinstance(ws, dict):
        return None
    return {
        "delta": ws.get("delta") or {},
        "applied": (ws.get("report") or {}).get("applied") or [],
    }


def _attempt_snapshot(entry) -> dict:
    return {k: entry[k] for k in _ATTEMPT_KEYS if isinstance(entry, dict) and k in entry}


def _entry_created_at(entry, fallback):
    """Returns the attempt's own timestamp, or the row's timestamp as a fallback.

    The value is cosmetic. Sibling order comes from `variant_index`, which this
    pass writes explicitly, so that nothing depends on two attempts made in the
    same second sorting in the order they were made.
    """
    raw = (entry or {}).get("created_at")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    if isinstance(fallback, str):
        try:
            return datetime.fromisoformat(fallback)
        except ValueError:
            return None
    return fallback


def _get_version(conn) -> int:
    if conn.dialect.name == "sqlite":
        return conn.execute(text("PRAGMA user_version")).scalar() or 1
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    ))
    version = conn.execute(text("SELECT version FROM schema_version")).scalar()
    # An existing database with no stamp can only have been created by an
    # earlier `create_all` of this same codebase, so it is already at LATEST.
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


def bootstrap(engine: Engine, through: int = LATEST_VERSION) -> None:
    """Brings the database up to `through`, which defaults to the newest version.

    The app always takes the default. A test passes an older version when it
    asserts on something a later migration removes. A migration test that stops
    at the version it is about keeps reading the columns that migration wrote,
    rather than the schema those columns became several versions later.
    """
    fresh = not inspect(engine).get_table_names()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        if fresh:
            _set_version(conn, through)
            return
        current = _get_version(conn)
        for version, sql in MIGRATIONS:
            if current < version <= through:
                statement = _for_dialect(sql, conn.dialect.name)
                # Skip the DDL when it has already run. The data pass below it
                # still runs.
                if not (_column_already_there(conn, statement)
                        or _column_already_gone(conn, statement)):
                    conn.execute(text(statement))
                if version == WORLD_DELTA_VERSION:
                    _backfill_world_delta(conn)
                if version == VARIANT_COUNT_VERSION:
                    _backfill_variant_count(conn)
                if version == EMBEDDING_BLOB_VERSION:
                    _backfill_embedding_blob(conn)
                # This has to run between migration 43, which adds the column,
                # and migration 44, which drops the old one. The loop is one
                # transaction, so if this raises, the DROP rolls back with it
                # and the prompts are still there.
                if version == SNAPSHOT_COMPRESS_VERSION:
                    _backfill_context_snapshot(conn)
                if version == TREE_BACKFILL_VERSION:
                    _backfill_tree(conn)
                if version == CURSOR_ANCHOR_VERSION:
                    _backfill_cursor_anchors(conn)
                # The order matters. The split reads what the first pass wrote
                # for the rows it does not change, and overwrites it for the
                # rows it does, because an attempt's own outcome takes priority
                # over one derived from the turn after it.
                if version == SIBLING_SPLIT_VERSION:
                    _backfill_state_after(conn)
                    _split_variants_into_siblings(conn)
                # Migration 63 adds the column and migration 64 indexes it.
                # The UPDATE is what the index serves, so it runs once both
                # exist.
                if version == PARENT_BACKFILL_VERSION:
                    _backfill_parents(conn)
                current = version
        _set_version(conn, current)
        _encrypt_plaintext_api_keys(conn)


def _encrypt_plaintext_api_keys(conn) -> None:
    """Encrypts API keys saved before encryption at rest existed (Phase 8).

    Those keys are stored in plain text, so this pass wraps them in Fernet. Plain
    SQL cannot do it. The pass runs on every start, and it matches no rows once
    every row carries the `enc:` prefix.
    """
    from . import security  # Deferred: security derives its key from DB_PATH setup.

    rows = conn.execute(text(
        "SELECT id, api_key FROM settings WHERE api_key != '' AND api_key NOT LIKE 'enc:%'"
    )).all()
    for row_id, plain in rows:
        conn.execute(
            text("UPDATE settings SET api_key = :key WHERE id = :id"),
            {"key": security.encrypt_secret(plain), "id": row_id},
        )
