"""Make a database look like an older schema version, so a migration can run.

`create_all` always builds the current schema. A test that wants to watch a
migration run must remove the newer columns first and then stamp an older
version. Otherwise the migration finds a column that already exists and
fails on a duplicate.

Rewinding the stamp alone worked for a while, so two test files did exactly
that. It stopped working when another `ADD COLUMN` migration landed. Without
this rewind, the replay runs migrations the tests never intended to
exercise, against columns `create_all` already added. This module rewinds
properly in one place. Adding a migration now means adding its inverse here,
instead of tracking down failures in three unrelated test files.

This module supports SQLite only. Every test that replays migrations runs on
a temp file, and SQLite stores the stamp in `PRAGMA user_version`.
Migrations that change a column's type (43-45, JSON to compressed bytes)
have no clean inverse, so this list omits them. Those migrations replay
as-is, which is what the tests using them already expect.
"""

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Each entry is (version that added the column, statements that remove it).
# The list is ordered newest first.
#
# Phase 14's `branch_id` columns are missing on purpose. SQLite refuses to
# drop a column that a foreign key references, so a current-schema database
# cannot be rewound past them. `migrations._column_already_there` handles
# this case. It skips DDL that already ran, so the tree migrations run their
# backfill against a schema that already has the columns.
_UNDO: list[tuple[int, tuple[str, ...]]] = [
    # Packed float32 vectors and the flag beside them.
    (39, ("ALTER TABLE memories DROP COLUMN embedded",)),
    (38, ("ALTER TABLE memories DROP COLUMN embedding_blob",)),
]


def rewind_to(engine: Engine, version: int) -> None:
    """Drop everything added after `version`, then stamp the database at it."""
    with engine.begin() as conn:
        for added_at, statements in _UNDO:
            if added_at > version:
                for sql in statements:
                    conn.execute(text(sql))
        conn.execute(text(f"PRAGMA user_version = {version}"))
