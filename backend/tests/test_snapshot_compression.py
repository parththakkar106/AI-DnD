"""context_snapshot, stored compressed.

The column makes up 89% of the database, and the free tier allows only
512 MB. Deferring the column already solved the read cost, so this is
about the storage ceiling. Postgres already TOASTs the column and gets
only a 1.7x ratio, because pglz favors fast decompression for data a query
might filter on. Nothing ever filters on an assembled prompt.

Three things must hold, and only the first is obvious:

* What goes in comes back out exactly, including a snapshot written before
  the conversion and one that is NULL.
* The model still hands callers a dict, so no call site changes.
* Migration 44 drops the original column, so the backfill is the one
  destructive step in this file. It must convert every row or abort.

    python -m pytest tests/test_snapshot_compression.py -v
"""
import json
import random
import zlib


import pytest
from sqlalchemy import text

from app import compression, migrations, models
from app.database import Base, SessionLocal, engine
from tests import schema_rewind
from tools.fakeprose import prose


def snapshot(seed: int, nbytes: int = 60_000) -> dict:
    rng = random.Random(seed)
    return {
        "system": prose(rng, nbytes // 3),
        "story": prose(rng, nbytes - nbytes // 3),
        "world_state": {"delta": {"player.hp": -3}},
    }


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def adventure(db):
    user = models.User(is_guest=False, email="snap@example.com")
    db.add(user)
    db.flush()
    adv = models.Adventure(user_id=user.id, title="Cave", script_state={})
    db.add(adv)
    db.commit()
    return adv


# ------------------------------------------------------------------- packing

def test_pack_round_trips_exactly():
    value = snapshot(1)
    assert compression.unpack(compression.pack(value)) == value


def test_pack_handles_the_awkward_values():
    for value in ({}, {"a": None}, {"nested": {"deep": [1, 2, {"x": "é"}]}}):
        assert compression.unpack(compression.pack(value)) == value


def test_pack_actually_shrinks_prose():
    """The whole justification. If this ratio collapses, the migration is
    spending CPU for nothing."""
    value = snapshot(2, 200_000)
    raw = json.dumps(value, separators=(",", ":")).encode()
    packed = compression.pack(value)
    assert len(packed) < len(raw) / 3, (
        f"{len(raw):,} B compressed to {len(packed):,} B — under 3x"
    )


def test_unpack_rejects_nothing_it_wrote():
    packed = compression.pack({"a": "b"})
    assert compression.unpack(bytearray(packed)) == {"a": "b"}
    assert compression.unpack(memoryview(bytes(packed))) == {"a": "b"}


# --------------------------------------------------------------- the column

def test_the_column_stores_bytes_and_returns_a_dict(db, adventure):
    value = snapshot(3)
    action = models.Action(
        adventure_id=adventure.id, type="ai", text="t",
        context_snapshot=value,
    )
    db.add(action)
    db.commit()
    db.expire_all()

    assert action.context_snapshot == value

    stored = db.execute(
        text("SELECT context_snapshot FROM actions WHERE id = :id"),
        {"id": action.id},
    ).scalar()
    assert isinstance(stored, (bytes, bytearray, memoryview))
    assert json.loads(zlib.decompress(bytes(stored))) == value


def test_the_column_is_smaller_than_the_json_it_holds(db, adventure):
    value = snapshot(4, 200_000)
    action = models.Action(
        adventure_id=adventure.id, type="ai", text="t",
        context_snapshot=value,
    )
    db.add(action)
    db.commit()

    stored = db.execute(
        text("SELECT length(context_snapshot) FROM actions WHERE id = :id"),
        {"id": action.id},
    ).scalar()
    assert stored < len(json.dumps(value)) / 3


def test_null_stays_null(db, adventure):
    action = models.Action(
        adventure_id=adventure.id, type="do", text="t",
        context_snapshot=None,
    )
    db.add(action)
    db.commit()
    db.expire_all()
    assert action.context_snapshot is None


def test_an_unreadable_snapshot_reads_as_none_rather_than_raising(db, adventure):
    """One corrupt row must not return a 500 error for the turn that loads
    it. The snapshot is a debugging view. The story is what actually
    matters."""
    action = models.Action(
        adventure_id=adventure.id, type="ai", text="t",
        context_snapshot={"a": "b"},
    )
    db.add(action)
    db.commit()
    db.execute(
        text("UPDATE actions SET context_snapshot = :junk WHERE id = :id"),
        {"junk": b"not zlib at all", "id": action.id},
    )
    db.commit()
    db.expire_all()

    assert db.get(models.Action, action.id).context_snapshot is None


# ------------------------------------------------------- the upgrade in full

def as_json_column(db, rows: dict[int, dict]) -> None:
    """The pre-43 schema: context_snapshot as a JSON column, populated."""
    db.execute(text("ALTER TABLE actions DROP COLUMN context_snapshot"))
    db.execute(text("ALTER TABLE actions ADD COLUMN context_snapshot JSON"))
    for action_id, value in rows.items():
        db.execute(
            text("UPDATE actions SET context_snapshot = :v WHERE id = :id"),
            {"v": json.dumps(value), "id": action_id},
        )
    db.commit()


def seed_pre_43(db, adventure, count: int = 4) -> dict[int, dict]:
    ids = []
    for i in range(count):
        action = models.Action(
            adventure_id=adventure.id, type="ai", text=f"t{i}"
        )
        db.add(action)
        db.flush()
        ids.append(action.id)
    # One action with no snapshot at all, which must survive as NULL.
    plain = models.Action(
        adventure_id=adventure.id, type="do", text="look"
    )
    db.add(plain)
    db.commit()

    expected = {action_id: snapshot(action_id) for action_id in ids}
    as_json_column(db, expected)
    db.commit()
    # Take the later migrations' columns back off too, not just the stamp:
    # replaying 43-45 also replays everything appended after them.
    schema_rewind.rewind_to(engine, migrations.SNAPSHOT_COMPRESS_VERSION - 1)
    return expected


def test_bootstrap_converts_every_snapshot(db, adventure):
    expected = seed_pre_43(db, adventure)
    db.close()

    migrations.bootstrap(engine)

    check = SessionLocal()
    try:
        for action_id, value in expected.items():
            assert check.get(models.Action, action_id).context_snapshot == value
        assert check.execute(
            text("SELECT count(*) FROM actions WHERE context_snapshot IS NULL")
        ).scalar() == 1
    finally:
        check.close()


def test_bootstrap_leaves_the_column_named_context_snapshot(db, adventure):
    seed_pre_43(db, adventure)
    db.close()

    migrations.bootstrap(engine)

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(actions)"))}
    assert "context_snapshot" in columns
    assert "context_snapshot_z" not in columns, "the swap left the scratch column behind"


def test_the_backfill_aborts_rather_than_dropping_unconvertible_data(
    db, adventure, monkeypatch
):
    """Migration 44 destroys the original column. If anything fails to
    convert, the whole run must roll back with the column still there.
    Otherwise a bug in this file could lose someone's prompts."""
    seed_pre_43(db, adventure)
    db.close()

    def broken_unpack(_blob):
        return {"not": "what went in"}

    monkeypatch.setattr(compression, "unpack", broken_unpack)

    with pytest.raises(RuntimeError, match="round trip"):
        migrations.bootstrap(engine)

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(actions)"))}
        version = conn.execute(text("PRAGMA user_version")).scalar()
        surviving = conn.execute(
            text("SELECT count(*) FROM actions WHERE context_snapshot IS NOT NULL")
        ).scalar()

    assert "context_snapshot" in columns
    assert version < migrations.SNAPSHOT_COMPRESS_VERSION, \
        "the version advanced past a backfill that failed"
    assert surviving == 4, "the prompts did not survive the rollback"
