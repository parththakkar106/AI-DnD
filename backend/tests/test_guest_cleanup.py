"""Guest retention policy — app/cleanup.py.

Covers the two things that matter: that idle guests and their whole data
graph actually go, and that nothing else ever does.

    python -m pytest tests/test_guest_cleanup.py -v
"""
import os
import tempfile
from datetime import timedelta

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import cleanup, models
from app.database import Base
from app.migrations import bootstrap


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):
        # The whole policy leans on ON DELETE CASCADE; SQLite ignores every
        # one of them unless this is set (same as database.py does).
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    bootstrap(engine)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


NOW = models.utcnow().replace(tzinfo=None)


def make_user(db, *, days_idle=None, days_old=0, guest=True, email=None):
    """A user last seen `days_idle` ago (None = never seen, only created)."""
    user = models.User(
        is_guest=guest,
        email=email,
        password_hash=None if email is None else "x",
        created_at=NOW - timedelta(days=days_old),
        last_seen_at=None if days_idle is None else NOW - timedelta(days=days_idle),
    )
    db.add(user)
    db.commit()
    return user


def sweep(db):
    return cleanup.delete_stale_guests(db, now=NOW)


def alive(db, user_id):
    # A count, not db.get: the sweep deletes with synchronize_session=False, so
    # the session's identity map still holds the object and db.get would answer
    # from memory without ever asking the database.
    return db.query(models.User).filter(models.User.id == user_id).count() == 1


# ---------- what goes ----------

def test_deletes_guest_idle_past_the_window(db):
    user = make_user(db, days_idle=6)
    assert sweep(db) == 1
    assert not alive(db, user.id)


def test_keeps_guest_inside_the_window(db):
    user = make_user(db, days_idle=4)
    assert sweep(db) == 0
    assert alive(db, user.id)


def test_boundary_is_not_yet_stale(db):
    # Exactly 5 days survives; the comparison is strict.
    user = make_user(db, days_idle=cleanup.RETENTION_DAYS)
    assert sweep(db) == 0
    assert alive(db, user.id)


def test_never_seen_guest_falls_back_to_created_at(db):
    """last_seen_at is NULL until a guest's second request (auth._touch runs
    hourly), so a coalesce-less query would delete brand-new visitors."""
    fresh = make_user(db, days_idle=None, days_old=0)
    stale = make_user(db, days_idle=None, days_old=9)
    assert sweep(db) == 1
    assert alive(db, fresh.id)
    assert not alive(db, stale.id)


def test_recent_visit_beats_an_old_created_at(db):
    # A long-standing guest who came back yesterday stays.
    user = make_user(db, days_idle=1, days_old=90)
    assert sweep(db) == 0
    assert alive(db, user.id)


# ---------- what must never go ----------

def test_spares_registered_users(db):
    """Registering upgrades the guest row in place, so an idle account here is
    a real user with real data — the whole point of signing up."""
    user = make_user(db, days_idle=400, guest=False, email="a@b.com")
    assert sweep(db) == 0
    assert alive(db, user.id)


def test_spares_the_local_mode_user(db):
    # email NULL but is_guest False: local mode's implicit owner of everything.
    user = make_user(db, days_idle=400, guest=False)
    assert sweep(db) == 0
    assert alive(db, user.id)


def test_spares_a_guest_flagged_row_that_has_an_email(db):
    # Shouldn't exist, but both clauses are checked so it can't be collected.
    user = make_user(db, days_idle=400, guest=True, email="odd@b.com")
    assert sweep(db) == 0
    assert alive(db, user.id)


def test_leaves_seeded_public_scenarios_alone(db):
    """Seeded demo content has user_id NULL, so it is outside the filter."""
    seeded = models.Scenario(user_id=None, is_public=True, title="Demo")
    db.add(seeded)
    make_user(db, days_idle=30)
    db.commit()
    assert sweep(db) == 1
    assert db.query(models.Scenario).filter(models.Scenario.id == seeded.id).count() == 1


def test_disabled_when_retention_is_zero(db, monkeypatch):
    monkeypatch.setattr(cleanup, "RETENTION_DAYS", 0)
    user = make_user(db, days_idle=999)
    assert sweep(db) == 0
    assert alive(db, user.id)


def test_enabled_requires_multi_user(monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "MULTI_USER", False)
    assert cleanup.enabled() is False
    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(cleanup, "RETENTION_DAYS", 5)
    assert cleanup.enabled() is True
    monkeypatch.setattr(cleanup, "RETENTION_DAYS", 0)
    assert cleanup.enabled() is False


# ---------- the cascade ----------

def test_deletes_the_whole_data_graph(db):
    """One DELETE has to take the adventure, its actions and memories, the
    story cards and the settings row with it — nothing is loaded into Python,
    so if the FK cascade isn't reaching, rows are silently orphaned (or the
    statement errors) rather than tidied."""
    user = make_user(db, days_idle=30)
    scenario = models.Scenario(user_id=user.id, title="S")
    db.add(scenario)
    db.commit()
    adventure = models.Adventure(user_id=user.id, scenario_id=scenario.id, title="A")
    db.add(adventure)
    db.commit()
    db.add_all([
        models.Action(adventure_id=adventure.id, index=0, type="ai", text="t"),
        models.Memory(adventure_id=adventure.id, text="m", source_start=0, source_end=0),
        models.StoryCard(adventure_id=adventure.id, name="c"),
        models.Settings(user_id=user.id),
    ])
    db.commit()

    assert sweep(db) == 1

    for model in (models.Scenario, models.Adventure, models.Action,
                  models.Memory, models.StoryCard, models.Settings):
        assert db.query(model).count() == 0, f"{model.__name__} rows survived"


def test_one_users_cleanup_does_not_touch_another(db):
    keeper = make_user(db, days_idle=1)
    keep_adv = models.Adventure(user_id=keeper.id, title="mine")
    goner = make_user(db, days_idle=30)
    db.add_all([keep_adv, models.Adventure(user_id=goner.id, title="theirs")])
    db.commit()

    assert sweep(db) == 1
    remaining = db.query(models.Adventure).all()
    assert [a.title for a in remaining] == ["mine"]


def test_sweep_swallows_errors(monkeypatch):
    """A broken cleanup must not take the app down (same rule as seeding)."""
    monkeypatch.setattr(cleanup.auth, "MULTI_USER", True)
    monkeypatch.setattr(cleanup, "delete_stale_guests",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cleanup.sweep() == 0
