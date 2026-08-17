"""Storing a JSON column compressed.

`actions.context_snapshot` holds the entire assembled prompt for a turn. It is
89% of the database — 150.8 MB of JSON across 944 actions on production, and
232 KB a row on the longest adventure — and the free tier this deploys to
allows 512 MB. Reads are not the problem: the column is deferred, so a page
load never touches it and exactly one endpoint fetches one row of it at a
time. Storage is the problem, and storage has a cliff.

Postgres already compresses it. TOAST brings 150.8 MB down to ~89 MB, a factor
of 1.7 — pglz is chosen for decompression speed on data a query might filter
on, which this never is. Nothing filters on a prompt; it is written once and
read whole, occasionally, by one screen. zlib at the application layer gets
three to four times on the same text, and the cost is a decompress on a
request that already costs an LLM call.

Doing it as a TypeDecorator rather than a second column keeps every call site
writing `action.context_snapshot = {...}` and reading a dict back, and keeps
`deferred=True`, `undefer()` and `load_only()` naming the same attribute they
named before. The storage format changes; nothing else does.

Level 6 is zlib's default and the knee of the curve here: 9 spends noticeably
more CPU on prompt text for about a percent more space.
"""
from __future__ import annotations

import json
import zlib

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

LEVEL = 6


def pack(value) -> bytes:
    """A JSON-able value as compressed UTF-8."""
    raw = json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")
    return zlib.compress(raw, LEVEL)


def unpack(blob: bytes) -> object:
    """The value `pack` was given."""
    return json.loads(zlib.decompress(bytes(blob)).decode("utf-8"))


class CompressedJSON(TypeDecorator):
    """A JSON column stored as zlib-compressed UTF-8 in a BLOB/BYTEA.

    `cache_ok = True`: the type carries no per-instance configuration, so
    SQLAlchemy may reuse a compiled statement across instances of it.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else pack(value)

    def process_result_value(self, value, dialect):
        # Tolerate a row the backfill has not reached yet, or one written
        # before the conversion: a snapshot that cannot be read back is worth
        # less than the screen that shows it, and never worth a 500 on the
        # turn that happens to load it.
        if value is None:
            return None
        try:
            return unpack(value)
        except (zlib.error, UnicodeDecodeError, ValueError):
            return None
