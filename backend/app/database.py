import os
import time
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# AIDND_DB_PATH lets deployments (Docker volume, hosted disk) relocate the
# SQLite database; default stays backend/data.db for local runs. The parent
# directory also hosts the auto-generated secret.key (see security.py), so
# DB_PATH stays defined even when Postgres is in use.
_env_db_path = os.environ.get("AIDND_DB_PATH")
DB_PATH = (
    Path(_env_db_path).resolve()
    if _env_db_path
    else Path(__file__).resolve().parent.parent / "data.db"
)

# `AIDND_DATABASE_URL`, or the conventional `DATABASE_URL`, switches the app to
# a server database. Any SQLAlchemy URL works, and hosted deploys use Postgres,
# which Phase 9 settled on Neon for. If neither variable is set, the app uses
# SQLite.
DATABASE_URL = (
    os.environ.get("AIDND_DATABASE_URL", "").strip()
    or os.environ.get("DATABASE_URL", "").strip()
)


def _normalize_url(url: str) -> str:
    """Map the postgres:// / postgresql:// schemes hosts hand out to the
    psycopg3 driver installed in requirements.txt."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


if DATABASE_URL:
    engine = create_engine(
        _normalize_url(DATABASE_URL),
        # Serverless Postgres (Neon) suspends idle databases; pre-ping
        # replaces silently-dead pooled connections instead of erroring.
        pool_pre_ping=True,
        # Store/read naive UTC like SQLite does, regardless of server default.
        connect_args={"options": "-c timezone=UTC"},
    )
else:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _record):
        # SQLite ships with foreign keys OFF per connection; without this every
        # ondelete=CASCADE/SET NULL in models.py is silently ignored.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# Per-request database timing for the `Server-Timing` header. The middleware in
# `main.py` sets a fresh dict here. Sync routes run in a threadpool with a copy
# of the context, so they update the same dict and the totals reach the
# response. Pool pre-pings bypass these events, so they count as app time.
db_timing: ContextVar[dict | None] = ContextVar("db_timing", default=None)


@event.listens_for(engine, "before_cursor_execute")
def _start_query(conn, _cursor, _statement, _params, _context, _executemany):
    conn.info["query_start"] = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _end_query(conn, _cursor, _statement, _params, _context, _executemany):
    timing = db_timing.get()
    start = conn.info.pop("query_start", None)
    if timing is not None and start is not None:
        timing["queries"] += 1
        timing["query_ms"] += (time.perf_counter() - start) * 1000


# A custom pool creator (`tools/dbmeter.py`) passes no connection record to
# `do_connect`, so the connect start time lives in the request's dict.
@event.listens_for(engine, "do_connect")
def _start_connect(_dialect, _conn_rec, _cargs, _cparams):
    timing = db_timing.get()
    if timing is not None:
        timing["connect_start"] = time.perf_counter()


@event.listens_for(engine, "connect")
def _end_connect(_dbapi_connection, _conn_rec):
    timing = db_timing.get()
    start = timing.pop("connect_start", None) if timing is not None else None
    if start is not None:
        timing["connects"] += 1
        timing["connect_ms"] += (time.perf_counter() - start) * 1000


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
