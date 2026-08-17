"""Make a database look like an older schema version, so a migration can run.

`create_all` always builds the *current* schema. A test that wants to watch a
migration happen therefore has to take the newer columns back off before it
stamps an older version — otherwise the migration meets a table that already
has its column and dies on a duplicate.

Rewinding the stamp alone was enough for a while, which is why two test files
did exactly that. It stopped being enough the moment another `ADD COLUMN`
landed after theirs: the replay then runs migrations they never meant to
exercise, against columns `create_all` had already made. This module is that
rewind done properly, in one place, so appending a migration means adding its
inverse here rather than discovering three unrelated test failures.

SQLite only — every test that replays migrations runs on a temp file, and
`PRAGMA user_version` is where the stamp lives there. Migrations that change a
column's *type* (43–45, JSON to compressed bytes) have no clean inverse and are
not listed: they get replayed as-is, which is what the tests using them already
relied on.
"""

from sqlalchemy import text
from sqlalchemy.engine import Engine

# (version that added it, statements that take it back off), newest first.
#
# Phase 14's `branch_id` columns are deliberately absent: SQLite refuses to drop
# a column a foreign key names ("unknown column in foreign key definition"), so
# a current-schema database cannot be rewound past them at all. That is what
# `migrations._column_already_there` is for — the replay skips DDL that has
# already happened, so the tree migrations run their backfill against a schema
# that already has the columns, which is exactly the situation here.
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
