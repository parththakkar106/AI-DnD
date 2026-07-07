import os
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

# AIDND_DATABASE_URL (or the platform-conventional DATABASE_URL) switches the
# app to a server database — any SQLAlchemy URL works, but Postgres is what
# hosted deploys use (Phase 9 decision: Neon). Unset = SQLite, as always.
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


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
