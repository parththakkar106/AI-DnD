"""Migration 38: embeddings move from a JSON list to packed float32.

The conversion has to be exact, because nothing re-embeds. A memory whose
vector shifts is silently ranked wrong forever, with no error anywhere to
report it. These tests check that the numbers survive the round trip bit
for bit, and that the migration reaches every row, however many there are.

    python -m pytest tests/test_embedding_blob.py -v
"""
import os
import struct
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import json
import random

import pytest
from sqlalchemy import text

from app import memorybank, migrations, models, vectors
from app.database import Base, SessionLocal, engine
from tests import schema_rewind


def float32(value: float) -> float:
    """`value` as the double nearest to its float32 truncation. This is
    what an embedding endpoint's JSON actually holds."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def sample_vector(rng: random.Random, dims: int = 1536) -> list[float]:
    return [float32(rng.uniform(-1.0, 1.0)) for _ in range(dims)]


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
    user = models.User(is_guest=False, email="vectors@example.com")
    db.add(user)
    db.flush()
    adv = models.Adventure(user_id=user.id, title="Cave", script_state={})
    db.add(adv)
    db.commit()
    return adv


# ------------------------------------------------------------------- packing

def test_pack_round_trips_exactly():
    """Not "close enough": embedding endpoints compute in float32 and render
    that into JSON, so packing back to float32 must be lossless."""
    rng = random.Random(1)
    vector = sample_vector(rng)
    assert list(vectors.unpack(vectors.pack(vector))) == vector


def test_packed_vector_is_four_bytes_per_dimension():
    """1536 dims takes 6 KB packed, against about 31 KB as JSON."""
    vector = sample_vector(random.Random(2))
    blob = vectors.pack(vector)
    assert len(blob) == 1536 * 4
    assert len(blob) < len(json.dumps(vector).encode()) / 4


def test_pack_handles_the_extremes():
    vector = [float32(v) for v in (0.0, -0.0, 1.0, -1.0, 3.4028234663852886e38, 1e-38)]
    assert list(vectors.unpack(vectors.pack(vector))) == vector


def test_unpack_returns_a_compact_array():
    """These vectors stay in memory between turns, so the container type
    matters. An array("f") stores each component in 4 bytes, the same width
    as the column. A list of Python floats uses eight times that."""
    vector = sample_vector(random.Random(9))
    unpacked = vectors.unpack(vectors.pack(vector))
    assert unpacked.typecode == "f"
    assert unpacked.itemsize == 4
    assert len(unpacked) == len(vector)


def test_pack_rounds_a_value_float32_cannot_hold():
    """The exactness guarantee applies only to vectors that came from an
    embedding model, which computes in float32, not to arbitrary doubles.
    This test pins down that boundary, because it is where the round-trip
    claim holds."""
    assert vectors.unpack(vectors.pack([1e-38]))[0] != 1e-38
    assert vectors.unpack(vectors.pack([1e-38]))[0] == pytest.approx(1e-38)


def test_cosine_moved_but_still_reachable_from_memorybank():
    """Callers import it from memorybank. The math lives in vectors."""
    assert memorybank.cosine is vectors.cosine
    assert vectors.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert vectors.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert vectors.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0  # length mismatch


# -------------------------------------------------------------- set_vector

def test_set_vector_writes_the_blob_and_the_flag(db, adventure):
    """The two columns that describe a vector move together, or a reader that
    trusts `embedded` gets a NULL blob."""
    memory = models.Memory(adventure_id=adventure.id, text="a fact")
    db.add(memory)
    db.commit()

    vector = sample_vector(random.Random(3), dims=8)
    memorybank.set_vector(memory, vector)
    db.commit()
    db.expire_all()

    assert list(vectors.unpack(memory.embedding_blob)) == vector
    assert memory.embedded is True


def test_set_vector_none_clears_both(db, adventure):
    """Editing a memory's text drops its vector so the next pass re-embeds. A
    blob left behind would keep ranking the old text."""
    memory = models.Memory(adventure_id=adventure.id, text="a fact")
    db.add(memory)
    memorybank.set_vector(memory, sample_vector(random.Random(4), dims=8))
    db.commit()

    memorybank.set_vector(memory, None)
    db.commit()
    db.expire_all()

    assert memory.embedding_blob is None
    assert memory.embedded is False


# --------------------------------------------------------------- the backfill

def add_legacy_json_column(db) -> None:
    """Put `memories.embedding` back for the length of a test.

    Migration 42 dropped it, and the model no longer declares it, so
    `create_all` does not produce it. Everything below tests the upgrade
    from a database that still has the column, which is the only state
    where the backfill has any work to do. Re-adding it by hand keeps these
    tests honest about the schema they claim to start from.
    """
    db.execute(text("ALTER TABLE memories ADD COLUMN embedding JSON"))
    db.commit()


def seed_json_only(db, adventure, count: int, dims: int = 64) -> dict[int, list[float]]:
    """Memories as they exist before the migration: JSON vector, no blob."""
    add_legacy_json_column(db)
    rng = random.Random(count)
    expected = {}
    for i in range(count):
        vector = sample_vector(rng, dims)
        memory = models.Memory(adventure_id=adventure.id, text=f"fact {i}")
        db.add(memory)
        db.flush()
        # Raw, because the ORM no longer knows this column exists.
        db.execute(
            text("UPDATE memories SET embedding = :v WHERE id = :id"),
            {"v": json.dumps(vector), "id": memory.id},
        )
        expected[memory.id] = vector
    db.commit()
    db.execute(text("UPDATE memories SET embedding_blob = NULL, embedded = false"))
    db.commit()
    return expected


def test_backfill_converts_every_existing_vector(db, adventure):
    expected = seed_json_only(db, adventure, count=5)

    with engine.begin() as conn:
        migrations._backfill_embedding_blob(conn)

    db.expire_all()
    for memory in db.query(models.Memory).all():
        assert list(vectors.unpack(memory.embedding_blob)) == expected[memory.id]


def test_backfill_reaches_past_one_batch(db, adventure):
    """It loops on id, and an off-by-one there would silently leave the
    tail of a big bank unconverted. That failure reads as "not embedded
    yet"."""
    count = migrations.BACKFILL_BATCH * 2 + 3
    expected = seed_json_only(db, adventure, count=count, dims=4)

    with engine.begin() as conn:
        migrations._backfill_embedding_blob(conn)

    db.expire_all()
    memories = db.query(models.Memory).all()
    assert len(memories) == count
    assert all(m.embedding_blob is not None for m in memories)
    assert all(list(vectors.unpack(m.embedding_blob)) == expected[m.id] for m in memories)


def test_backfill_leaves_unembedded_memories_alone(db, adventure):
    add_legacy_json_column(db)
    db.add(models.Memory(adventure_id=adventure.id, text="not embedded yet"))
    db.commit()

    with engine.begin() as conn:
        migrations._backfill_embedding_blob(conn)

    db.expire_all()
    assert db.query(models.Memory).one().embedding_blob is None


def test_backfill_is_idempotent(db, adventure):
    """It runs once from bootstrap, but a half-finished run must be safe to
    repeat, and rows already converted must not be rewritten."""
    seed_json_only(db, adventure, count=3)

    with engine.begin() as conn:
        migrations._backfill_embedding_blob(conn)
    db.expire_all()
    first = {m.id: m.embedding_blob for m in db.query(models.Memory).all()}

    with engine.begin() as conn:
        migrations._backfill_embedding_blob(conn)
    db.expire_all()
    assert {m.id: m.embedding_blob for m in db.query(models.Memory).all()} == first


def test_backfill_skips_a_malformed_row_without_stopping(db, adventure):
    """One bad row must not strand every row after it. The loop orders by
    id, so an exception here would leave the rest of the bank unconverted."""
    expected = seed_json_only(db, adventure, count=2)
    broken = models.Memory(adventure_id=adventure.id, text="broken")
    db.add(broken)
    db.commit()
    db.execute(
        text("UPDATE memories SET embedding = :bad WHERE id = :id"),
        {"bad": '"not a list"', "id": broken.id},
    )
    db.commit()

    with engine.begin() as conn:
        migrations._backfill_embedding_blob(conn)

    db.expire_all()
    assert db.get(models.Memory, broken.id).embedding_blob is None
    for memory_id, vector in expected.items():
        blob = db.get(models.Memory, memory_id).embedding_blob
        assert list(vectors.unpack(blob)) == vector


# ------------------------------------------------------- the upgrade in full

def test_bootstrap_adds_the_columns_and_backfills_them(db, adventure):
    """The path a deployed database actually takes: sitting at 37 with neither
    new column, then started on this build."""
    expected = seed_json_only(db, adventure, count=4)
    unembedded = models.Memory(adventure_id=adventure.id, text="no vector yet")
    db.add(unembedded)
    db.commit()
    unembedded_id = unembedded.id
    db.close()

    schema_rewind.rewind_to(engine, migrations.EMBEDDING_BLOB_VERSION - 1)

    migrations.bootstrap(engine)

    with engine.begin() as conn:
        assert conn.execute(text("PRAGMA user_version")).scalar() == migrations.LATEST_VERSION
        rows = conn.execute(text("SELECT id, embedding_blob, embedded FROM memories")).all()
    by_id = {row[0]: (row[1], row[2]) for row in rows}
    assert len(by_id) == len(expected) + 1
    for memory_id, vector in expected.items():
        blob, embedded = by_id[memory_id]
        assert list(vectors.unpack(blob)) == vector
        assert embedded
    # The flag has to follow the vector, not the row: a memory that was never
    # embedded must still read as not embedded afterwards.
    assert by_id[unembedded_id] == (None, False)

    # Migration 42, at the end of the same run, removes the JSON column.
    # Ordering matters: 38 reads it, 42 drops it, and an upgrade that ran
    # them in the other order would arrive with an empty bank.
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(memories)"))}
    assert "embedding" not in columns
    assert {"embedding_blob", "embedded"} <= columns


def test_migration_38_is_spelled_for_both_dialects():
    """Every Postgres deploy replays migrations from 24 on, so a SQLite-only
    ALTER here would break the live database and nothing else would notice."""
    sql = dict(migrations.MIGRATIONS)[migrations.EMBEDDING_BLOB_VERSION]
    assert "BLOB" in migrations._for_dialect(sql, "sqlite")
    assert "BYTEA" in migrations._for_dialect(sql, "postgresql")
