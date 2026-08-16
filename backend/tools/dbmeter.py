"""How many bytes the database was actually asked for.

Query *counts* are easy to see and have never been the problem here. Both
egress blowouts this project has had were one query fetching a column nobody
read: `context_snapshot` on every action, then every memory's embedding on
every turn. Counting statements would have shown nothing wrong in either case,
and at development scale — ten rows, no embeddings — so would a stopwatch.

So this meter measures bytes, and it measures them at the only place the truth
is available: the DBAPI cursor, after the driver has decoded a row and before
SQLAlchemy has turned it into anything. Every value that crosses that line
crossed the wire.

Usage:

    meter = Meter()
    meter.attach(engine)
    with meter.scope("turn"):
        ...                      # anything that touches the database
    print(meter.render())

`attach()` wraps the pool's connection factory, so it reuses the engine the app
already configured rather than rebuilding one beside it — nothing about
connect args, pre-ping or the SQLite foreign-key pragma has to be repeated
here, and drift between the metered engine and the real one is impossible.
Existing pooled connections are dropped first, so a connection opened before
attaching cannot quietly stay unmetered.

Sizes are of the decoded values, not of the protocol framing: the count is what
the payload weighs, and ignores per-row and per-packet overhead. Text and bytes
are exact. Numbers are charged their binary width, which is what the Postgres
binary protocol sends and close enough elsewhere. JSON is the case that matters
most and the one to be careful about: SQLite hands back the raw string (exact),
while psycopg parses `json`/`jsonb` into Python before this sees it, so the
value is re-serialised to size it. Re-serialising is within a byte or two of
the original for machine-written JSON, which is all this project stores.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field

# The table a statement is charged to. Only the first match is used: a join
# reads mostly from its driving table, and splitting a row across tables would
# need the result metadata, which is more machinery than the answer is worth.
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+\"?([A-Za-z_][A-Za-z_0-9]*)\"?", re.IGNORECASE
)
_WHITESPACE_RE = re.compile(r"\s+")


def value_bytes(value) -> int:
    """The payload weight of one decoded column value."""
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, memoryview):
        return value.nbytes
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return 8
    if isinstance(value, (dict, list)):
        # A JSON column the driver already parsed (psycopg does; SQLite does
        # not). Separators match what a database emits — no spaces.
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    return len(str(value).encode("utf-8"))


def row_bytes(row) -> int:
    return sum(value_bytes(v) for v in row)


def _table_of(statement: str) -> str:
    match = _TABLE_RE.search(statement)
    if match:
        return match.group(1).lower()
    # No table to name — BEGIN, a PRAGMA, a savepoint. Group those under the
    # keyword so they stay countable instead of collapsing into one "?" bucket.
    head = statement.strip().split(None, 1)
    return head[0].lower()[:20] if head else "(empty)"


def _one_line(statement: str, width: int = 132) -> str:
    collapsed = _WHITESPACE_RE.sub(" ", statement).strip()
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "…"


@dataclass
class Tally:
    statements: int = 0
    rows: int = 0
    fetched: int = 0  # bytes


@dataclass
class Scope:
    """What one measured stretch of work asked the database for."""

    name: str
    total: Tally = field(default_factory=Tally)
    by_table: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    by_statement: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))

    def add_statement(self, statement: str) -> None:
        self.total.statements += 1
        self.by_table[_table_of(statement)].statements += 1
        self.by_statement[statement].statements += 1

    def add_rows(self, statement: str, rows: int, nbytes: int) -> None:
        for tally in (
            self.total,
            self.by_table[_table_of(statement)],
            self.by_statement[statement],
        ):
            tally.rows += rows
            tally.fetched += nbytes


class Meter:
    """Collects what the metered cursors report, grouped by scope.

    Scopes nest: a statement is charged to every scope currently open, so an
    inner "retrieve memories" and an outer "turn" both see it.
    """

    def __init__(self) -> None:
        self._open: list[Scope] = []
        self.scopes: list[Scope] = []
        self._attached_pools: list = []

    # -------------------------------------------------------------- recording

    @contextmanager
    def scope(self, name: str):
        scope = Scope(name)
        self._open.append(scope)
        try:
            yield scope
        finally:
            self._open.pop()
            self.scopes.append(scope)

    def note_statement(self, statement: str) -> None:
        for scope in self._open:
            scope.add_statement(statement)

    def note_rows(self, statement: str, rows: int, nbytes: int) -> None:
        for scope in self._open:
            scope.add_rows(statement, rows, nbytes)

    # ------------------------------------------------------------- attaching

    def attach(self, engine) -> None:
        """Meter every connection this engine opens from now on."""
        # Drop pooled connections created before now; they were built by the
        # unwrapped creator and would go on reporting nothing.
        engine.dispose()
        pool = engine.pool
        creator = pool._creator  # the factory create_engine() built from the URL
        if getattr(creator, "_dbmeter", None) is self:
            return
        meter = self

        def metered_creator():
            return _MeteredConnection(creator(), meter)

        metered_creator._dbmeter = self
        pool._creator = metered_creator
        self._attached_pools.append(pool)

    # ------------------------------------------------------------- reporting

    def render(self, *, statements: int = 5) -> str:
        return "\n".join(render_scope(s, statements=statements) for s in self.scopes)


# ------------------------------------------------------------ DBAPI wrappers


class _MeteredConnection:
    """A DBAPI connection that hands out metered cursors.

    Everything else is delegated: the dialects reach for driver-specific
    attributes (`isolation_level` on SQLite, `info` and `autocommit` on
    psycopg) and this must stay transparent to all of them.
    """

    def __init__(self, connection, meter: Meter) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_meter", meter)

    def cursor(self, *args, **kwargs):
        return _MeteredCursor(self._connection.cursor(*args, **kwargs), self._meter)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_connection"), name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *exc):
        return self._connection.__exit__(*exc)


class _MeteredCursor:
    """Counts the bytes of every row handed back.

    The fetch methods are wrapped rather than `execute`, because what a
    statement *costs* is not knowable when it is sent — `SELECT * FROM
    memories` and `SELECT count(*) FROM memories` look alike going out and
    differ by three megabytes coming back.
    """

    def __init__(self, cursor, meter: Meter) -> None:
        self.__dict__["_cursor"] = cursor
        self.__dict__["_meter"] = meter
        self.__dict__["_statement"] = ""

    # -- execution

    def execute(self, statement, *args, **kwargs):
        self.__dict__["_statement"] = statement
        self._meter.note_statement(statement)
        return self._cursor.execute(statement, *args, **kwargs)

    def executemany(self, statement, *args, **kwargs):
        self.__dict__["_statement"] = statement
        self._meter.note_statement(statement)
        return self._cursor.executemany(statement, *args, **kwargs)

    # -- fetching

    def _charge(self, rows) -> None:
        self._meter.note_rows(
            self._statement, len(rows), sum(row_bytes(r) for r in rows)
        )

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is not None:
            self._charge([row])
        return row

    def fetchmany(self, *args, **kwargs):
        rows = self._cursor.fetchmany(*args, **kwargs)
        self._charge(rows)
        return rows

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._charge(rows)
        return rows

    def __iter__(self):
        # Special methods are looked up on the type, so this cannot be left to
        # __getattr__ the way the rest of the driver surface is.
        for row in self._cursor:
            self._charge([row])
            yield row

    def __getattr__(self, name):
        return getattr(self.__dict__["_cursor"], name)

    def __setattr__(self, name, value):
        setattr(self.__dict__["_cursor"], name, value)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)


# --------------------------------------------------------------- formatting


def kb(nbytes: int) -> str:
    return f"{nbytes / 1024:,.1f} kB"


def render_scope(scope: Scope, *, statements: int = 5) -> str:
    lines = [
        "",
        f"── {scope.name} " + "─" * max(0, 62 - len(scope.name)),
        f"   {scope.total.statements} statements · {scope.total.rows} rows · "
        f"{kb(scope.total.fetched)} fetched",
    ]

    if scope.by_table:
        lines += ["", f"   {'table':<20}{'stmts':>7}{'rows':>8}{'fetched':>16}"]
        ranked = sorted(
            scope.by_table.items(), key=lambda kv: kv[1].fetched, reverse=True
        )
        for table, tally in ranked:
            share = tally.fetched / scope.total.fetched if scope.total.fetched else 0
            lines.append(
                f"   {table:<20}{tally.statements:>7}{tally.rows:>8}"
                f"{kb(tally.fetched):>16}{share:>7.0%}"
            )

    heavy = sorted(
        scope.by_statement.items(), key=lambda kv: kv[1].fetched, reverse=True
    )[:statements]
    heavy = [(s, t) for s, t in heavy if t.fetched]
    if heavy:
        lines += ["", "   heaviest statements"]
        for statement, tally in heavy:
            lines.append(f"   {kb(tally.fetched):>14}  {tally.statements}x  {_one_line(statement)}")

    return "\n".join(lines)
