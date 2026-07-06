import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# AIDND_DB_PATH lets deployments (Docker volume, hosted disk) relocate the
# database; default stays backend/data.db for local runs.
_env_db_path = os.environ.get("AIDND_DB_PATH")
DB_PATH = (
    Path(_env_db_path).resolve()
    if _env_db_path
    else Path(__file__).resolve().parent.parent / "data.db"
)
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
