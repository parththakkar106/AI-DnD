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
import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from . import compression, vectors
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
    # context_snapshot, compressed. 89% of the database is one column holding
    # assembled prompts nobody filters on and one screen reads, one row at a
    # time; Postgres already TOASTs it, but pglz only manages 1.7x and zlib
    # gets three to four on the same text. Reads were fixed by deferring it —
    # this is about the 512 MB the free tier allows.
    #
    # Three steps because a column cannot portably change type in place: add
    # the new one, convert into it (_backfill_context_snapshot, which verifies
    # every row round-trips before the old column goes), then swap the names so
    # the model keeps calling it context_snapshot.
    #
    # **Postgres does not hand the disk back on its own.** DROP COLUMN only
    # marks the column dropped, and the backfill's UPDATE leaves a dead tuple
    # per row, so the table gets *bigger* before it gets smaller: peak is
    # roughly twice the starting size while both columns are live. Plain
    # autovacuum makes that space reusable but does not shrink the files. The
    # deploy that ships this should follow it with, once:
    #
    #     VACUUM FULL actions;
    #
    # which needs exclusive access and free space equal to the finished table.
    # On the 2026-08-17 figures that is 99.6 MB peaking near 200, settling at
    # about 53 once vacuumed, against a 512 MB tier. Skipping the vacuum is
    # safe and simply leaves the win unrealised.
    (43, {"sqlite": "ALTER TABLE actions ADD COLUMN context_snapshot_z BLOB",
          "default": "ALTER TABLE actions ADD COLUMN context_snapshot_z BYTEA"}),
    (44, "ALTER TABLE actions DROP COLUMN context_snapshot"),
    (45, "ALTER TABLE actions RENAME COLUMN context_snapshot_z TO context_snapshot"),
    # Phase 14, SP1: the story becomes a tree. Every action gains the branch it
    # was played on and its depth along that branch; adventures gain a head
    # pointer; memories attach to the node that produced them. The `branches`
    # table itself comes from create_all, like `memories` did.
    #
    # Nothing reads these yet — SP2 moves the reads onto them. This subphase
    # exists so that by the time anything does, every row already has them,
    # including the rows written between the two deploys (`app/tree.py` stamps
    # those). Legacy `index`, `variants`, `variant_index` and `variant_count`
    # stay in place, unread, until the tree is proven live.
    #
    # **This rewrites every row of `actions`, twice** — once per ADD COLUMN
    # backfill pass on Postgres — so the deploy that ships it must be followed
    # by, once:
    #
    #     VACUUM FULL actions;
    #
    # on the direct endpoint, not -pooler. That is the 144 MB lesson from
    # 2026-08-17: a rewrite roughly doubles the table and only a VACUUM FULL
    # hands the space back. Skipping it is safe and simply leaves the table fat.
    (46, "ALTER TABLE actions ADD COLUMN branch_id INTEGER "
         "REFERENCES branches(id) ON DELETE CASCADE"),
    (47, "ALTER TABLE actions ADD COLUMN depth INTEGER"),
    # head_branch_id carries no REFERENCES: branches.adventure_id already points
    # the other way, and two constraints would make the pair a cycle create_all
    # cannot order. See the column comment in models.py.
    (48, "ALTER TABLE adventures ADD COLUMN head_branch_id INTEGER"),
    (49, "ALTER TABLE adventures ADD COLUMN head_depth INTEGER NOT NULL DEFAULT -1"),
    (50, "ALTER TABLE memories ADD COLUMN branch_id INTEGER "
         "REFERENCES branches(id) ON DELETE CASCADE"),
    (51, "ALTER TABLE memories ADD COLUMN depth INTEGER"),
    # The index every branch clause wants, and the data pass that fills the six
    # columns above (_backfill_tree, hung off this version because it needs all
    # of them to exist).
    (52, "CREATE INDEX IF NOT EXISTS ix_actions_branch_depth ON actions (branch_id, depth)"),
    # Phase 14, SP3 — the memory and summary cursors stop being positions in the
    # story and become nodes in it: (branch, depth) of the last action each pass
    # covered. Legacy `memory_cursor` / `summary_cursor` stay, unread, until SP8
    # drops them beside `actions.index`.
    #
    # Unlike 46-52 this rewrites `adventures`, not `actions` — a few hundred
    # rows against a few hundred thousand — so it needs no VACUUM FULL of its
    # own. (The one SP1's deploy asks for is still owed.)
    (53, "ALTER TABLE adventures ADD COLUMN memory_cursor_branch_id INTEGER"),
    (54, "ALTER TABLE adventures ADD COLUMN memory_cursor_depth INTEGER NOT NULL DEFAULT -1"),
    (55, "ALTER TABLE adventures ADD COLUMN summary_cursor_branch_id INTEGER"),
    (56, "ALTER TABLE adventures ADD COLUMN summary_cursor_depth INTEGER NOT NULL DEFAULT -1"),
]

LATEST_VERSION = max((v for v, _ in MIGRATIONS), default=1)

# Migrations that need a data pass after their DDL, keyed by version.
WORLD_DELTA_VERSION = 36
VARIANT_COUNT_VERSION = 37
EMBEDDING_BLOB_VERSION = 38
SNAPSHOT_COMPRESS_VERSION = 43
TREE_BACKFILL_VERSION = 52
CURSOR_ANCHOR_VERSION = 56

# An adventure with no actions has no tip. -1 keeps "the next node goes at
# head_depth + 1" true without a special case (mirrors tree.NO_DEPTH).
NO_DEPTH = -1

# Snapshots converted per round trip. Deliberately far smaller than
# BACKFILL_BATCH: a vector is 6 KB and a snapshot is 232 KB, so 200 of these
# would be 46 MB held at once.
SNAPSHOT_BATCH = 50

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


# Matches the ADD COLUMN migrations in this file — all hand-written above, so
# this parses SQL we control and nothing else.
_ADD_COLUMN = re.compile(r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+\"?(\w+)\"?", re.I)


def _column_already_there(conn, sql: str) -> bool:
    """True when `sql` adds a column the table already has.

    This is the `IF NOT EXISTS` the docstring asks for, spelled in Python
    because SQLite has no syntax for it on ADD COLUMN. Without it, any database
    carrying a *newer* column than its stamp claims dies on a duplicate column
    with the backfill never running — and that database is not hypothetical:
    `create_all` always builds the current schema, so it is what every test
    replaying a migration starts from, and SQLite cannot drop the columns back
    off again once a foreign key names them.
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


def _backfill_context_snapshot(conn) -> None:
    """Compress actions.context_snapshot into actions.context_snapshot_z.

    Runs between migration 43 and 44, which is the only window where both
    columns exist. Migration 44 drops the original, so unlike every other
    backfill here this one is destructive if it is wrong — and every row it
    converts is somebody's game. So each row is decompressed again and
    compared against what went in before it counts as converted, and a row
    that fails to round-trip aborts the whole run rather than being skipped:
    the transaction rolls back, the DROP never happens, and the prompts are
    still there to try again.

    Reads the JSON the same defensive way as the vector backfill — SQLite
    hands back a raw string, psycopg has already parsed it.
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
            # One executemany per batch, not one statement per row. This runs
            # at container start, before the port opens, against a database on
            # the other end of a network: a thousand round trips is the
            # difference between a deploy that comes up and a health check that
            # times out waiting for it.
            conn.execute(
                text("UPDATE actions SET context_snapshot_z = :z WHERE id = :id"),
                params,
            )
        last_id = rows[-1][0]


# "The root branch of the adventure this row belongs to." MIN(id) rather than a
# LIMIT so it is a plain scalar subquery on both dialects, and deterministic if a
# database ever ends up with two roots for one adventure.
def _root_branch_of(column: str) -> str:
    return (
        "(SELECT MIN(b.id) FROM branches b "
        f"WHERE b.adventure_id = {column} AND b.parent_branch_id IS NULL)"
    )


def _backfill_tree(conn) -> None:
    """Re-read every existing adventure's linear story as a tree with one branch.

    One root branch per adventure, `depth` = the old `index`, the head pointing
    at the tip, and every memory hung off the node it summarised. Nothing is
    copied, nothing is deleted, and no ordering changes — `index` and `depth`
    hold the same numbers when this finishes, which is what makes "current
    adventures are unaffected" a testable claim rather than a hope.

    Server-side: `actions` is the table that fills the disk, and pulling it into
    Python to write two integers a row would be the same mistake this project
    has now made twice. Each statement is guarded on its own target being unset,
    so a run that dies halfway resumes rather than double-applying.
    """
    sqlite = conn.dialect.name == "sqlite"

    # 1. A root branch per adventure. Its lineage names the row's own id, which
    #    does not exist until the row does, so it starts as the empty list —
    #    `branches` comes from create_all, where lineage is NOT NULL, so an
    #    empty array is what "not filled in yet" has to look like.
    conn.execute(text("""
        INSERT INTO branches (adventure_id, parent_branch_id, fork_depth, lineage, created_at)
        SELECT a.id, NULL, NULL, '[]', CURRENT_TIMESTAMP
        FROM adventures a
        WHERE NOT EXISTS (SELECT 1 FROM branches b WHERE b.adventure_id = a.id)
    """))

    # 2. lineage = [[own_id, null]] — one entry, uncapped: the root branch is
    #    the whole story. Built by the database's own JSON functions because
    #    binding a JSON string as a parameter has no spelling that means the
    #    same thing to SQLite (TEXT) and to Postgres (json). The guard is a
    #    length, not `= '[]'`: Postgres `json` has no equality operator.
    conn.execute(text(
        "UPDATE branches SET lineage = json_array(json_array(id, null)) "
        "WHERE json_array_length(lineage) = 0"
        if sqlite else
        "UPDATE branches SET lineage = "
        "jsonb_build_array(jsonb_build_array(id, null))::json "
        "WHERE json_array_length(lineage) = 0"
    ))

    # 3. Every action onto that branch, at the depth its index already implies.
    #    Deleting a middle action left gaps in `index`, and those gaps carry
    #    over deliberately: depth has to keep the order the story is read in,
    #    and renumbering here would move every cursor that points past the gap.
    conn.execute(text(f"""
        UPDATE actions
        SET branch_id = {_root_branch_of('actions.adventure_id')},
            depth = "index"
        WHERE branch_id IS NULL
    """))

    # 4. The head: the root branch, and the depth of its newest node.
    conn.execute(text(f"""
        UPDATE adventures
        SET head_branch_id = {_root_branch_of('adventures.id')},
            head_depth = COALESCE(
                (SELECT MAX(a."index") FROM actions a WHERE a.adventure_id = adventures.id),
                {NO_DEPTH}
            )
        WHERE head_branch_id IS NULL
    """))

    # 5. Memories onto the node that produced them: `source_end` is the index of
    #    the last action a memory summarised, so it is that node's depth. A
    #    hand-written memory has no node and keeps depth NULL.
    conn.execute(text(f"""
        UPDATE memories
        SET branch_id = {_root_branch_of('memories.adventure_id')},
            depth = source_end
        WHERE branch_id IS NULL
    """))


# A frozen copy of `context.history._STORY_TEXT` as it stood at version 56:
# "text that is not blank once whitespace is stripped". It is written out here
# rather than imported because a migration has to keep meaning what it meant on
# the day it ran, while the module is free to move. `char()` is `chr()` on
# Postgres and there is no third spelling, so it takes a dialect map.
def _story_text_sql(column: str, sqlite: bool) -> str:
    char = "char" if sqlite else "chr"
    folded = column
    for code in (10, 13, 9):  # newline, carriage return, tab
        folded = f"replace({folded}, {char}({code}), ' ')"
    return f"trim({folded}) <> ''"


def _backfill_cursor_anchors(conn) -> None:
    """Read each adventure's two cursors as nodes instead of as positions.

    `memory_cursor` = 12 meant "the first twelve story actions are covered".
    The twelfth story action, in depth order, is the node that says the same
    thing and goes on saying it after something in front of it is deleted — so
    the translation is a `ROW_NUMBER()` over the story and a lookup at the
    cursor's own value.

    Two cases the arithmetic has to survive:

    * **A cursor past the end of the story.** Legitimate — an adventure caught
      up under the older rule can have a cursor equal to its action count, and
      `run_post_turn` used to clamp it every pass. There is no `rn` to match,
      so it falls back to the deepest node there is: still "caught up", which
      is what the number meant.
    * **A cursor of 0**, which is most adventures. Nothing covered, the column
      default already says so, and no row is touched.

    Guarded on `_depth = -1` so a run that dies halfway resumes: every
    adventure this has already converted is skipped, and one it has not is
    indistinguishable from an untouched row.
    """
    sqlite = conn.dialect.name == "sqlite"
    story = _story_text_sql("text", sqlite)
    for name in ("memory", "summary"):
        conn.execute(text(f"""
            UPDATE adventures
            SET {name}_cursor_branch_id = {_root_branch_of('adventures.id')},
                {name}_cursor_depth = COALESCE(
                    (SELECT ranked.depth FROM (
                        SELECT adventure_id, depth, ROW_NUMBER() OVER (
                            PARTITION BY adventure_id ORDER BY depth, id
                        ) AS rn
                        FROM actions WHERE {story}
                     ) AS ranked
                     WHERE ranked.adventure_id = adventures.id
                       AND ranked.rn = adventures.{name}_cursor),
                    (SELECT MAX(a.depth) FROM actions a
                      WHERE a.adventure_id = adventures.id
                        AND {_story_text_sql('a.text', sqlite)}),
                    {NO_DEPTH})
            WHERE adventures.{name}_cursor > 0
              AND adventures.{name}_cursor_depth = {NO_DEPTH}
        """))


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
                statement = _for_dialect(sql, conn.dialect.name)
                # The DDL is skippable when it has already happened; the data
                # pass below it is not, and still runs.
                if not _column_already_there(conn, statement):
                    conn.execute(text(statement))
                if version == WORLD_DELTA_VERSION:
                    _backfill_world_delta(conn)
                if version == VARIANT_COUNT_VERSION:
                    _backfill_variant_count(conn)
                if version == EMBEDDING_BLOB_VERSION:
                    _backfill_embedding_blob(conn)
                # Must land between 43 (add the column) and 44 (drop the old
                # one). The loop is one transaction, so if this raises, the
                # DROP rolls back with it and the prompts are still there.
                if version == SNAPSHOT_COMPRESS_VERSION:
                    _backfill_context_snapshot(conn)
                if version == TREE_BACKFILL_VERSION:
                    _backfill_tree(conn)
                if version == CURSOR_ANCHOR_VERSION:
                    _backfill_cursor_anchors(conn)
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
