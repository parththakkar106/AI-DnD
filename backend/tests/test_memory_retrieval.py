"""Ranking the memory bank without reading the memory bank.

Retrieval used to walk `adventure.memories`, which loaded every row with
its vector. That vector data was 96% of everything a turn read. Retrieval
now asks SQL which memories are in play, holds their vectors in process,
and fetches text for only the five it picks.

Three things have to stay true for that to be safe, and each is a separate
failure that no error message would report:

* the ranking picks the same memories it always did;
* nothing bulk-reads a vector column again;
* a cached vector is never served after the stored one changed.

    python -m pytest tests/test_memory_retrieval.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, inspect as sa_inspect

from app import memorybank, models
from app.database import Base, SessionLocal, engine


class StubEmbedder:
    """Returns whatever vector the test set, and counts calls."""

    def __init__(self, vector=(1.0, 0.0, 0.0)):
        self.vector = list(vector)
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [list(self.vector) for _ in texts]


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    memorybank._vector_cache.clear()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        memorybank._vector_cache.clear()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def settings(db):
    user = models.User(is_guest=False, email="rank@example.com")
    db.add(user)
    db.flush()
    row = models.Settings(
        user_id=user.id, api_key="enc:dummy", model="m",
        embedding_model="text-embedding-3-small", memory_top_k=2,
        memory_bank_capacity=80,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def adventure(db, settings):
    adv = models.Adventure(
        user_id=settings.user_id, title="Cave", script_state={},
        memory_bank_enabled=True,
    )
    db.add(adv)
    db.flush()
    # Retrieval builds its query from the newest actions. With none, it
    # returns before ranking anything.
    for i in range(2):
        db.add(models.Action(
            adventure_id=adv.id, index=i, type="ai", text=f"Something happened {i}."
        ))
    db.commit()
    return adv


def add_memory(db, adventure, text, vector, **kwargs):
    memory = models.Memory(adventure_id=adventure.id, text=text, **kwargs)
    db.add(memory)
    db.flush()
    if vector is not None:
        memorybank.set_vector(memory, list(vector))
    db.commit()
    return memory


@pytest.fixture()
def bank(db, adventure):
    """Three orthogonal vectors, so a query vector picks one unambiguously."""
    return {
        "x": add_memory(db, adventure, "about x", (1.0, 0.0, 0.0)),
        "y": add_memory(db, adventure, "about y", (0.0, 1.0, 0.0)),
        "z": add_memory(db, adventure, "about z", (0.0, 0.0, 1.0)),
    }


def retrieve(adventure, settings, embedder, **kwargs):
    memorybank.embedding_provider = lambda s: embedder
    kwargs.setdefault("update_stats", False)
    return asyncio.run(memorybank.retrieve_memories(adventure, settings, **kwargs))


@pytest.fixture()
def sql_log():
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


# ------------------------------------------------------------------- ranking

def test_ranks_by_cosine_similarity(db, adventure, settings, bank):
    result = retrieve(adventure, settings, StubEmbedder((1.0, 0.0, 0.0)))
    assert [m["id"] for m in result["used"]][0] == bank["x"].id
    assert result["used"][0]["similarity"] == pytest.approx(1.0)
    assert result["used"][0]["text"] == "about x"


def test_honours_top_k(db, adventure, settings, bank):
    settings.memory_top_k = 1
    db.commit()
    assert len(retrieve(adventure, settings, StubEmbedder())["used"]) == 1


def test_pinned_memories_are_always_used(db, adventure, settings, bank):
    """A pin means "always in context", however badly it scores."""
    settings.memory_top_k = 1
    bank["z"].pinned = True
    db.commit()

    used = retrieve(adventure, settings, StubEmbedder((1.0, 0.0, 0.0)))["used"]
    assert [m["id"] for m in used] == [bank["z"].id]
    assert used[0]["pinned"] is True


def test_forgotten_and_unembedded_memories_are_not_candidates(db, adventure, settings):
    live = add_memory(db, adventure, "live", (1.0, 0.0, 0.0))
    add_memory(db, adventure, "evicted", (1.0, 0.0, 0.0), forgotten=True)
    add_memory(db, adventure, "no vector yet", None)

    used = retrieve(adventure, settings, StubEmbedder())["used"]
    assert [m["id"] for m in used] == [live.id]


def test_empty_bank_returns_no_error(db, adventure, settings):
    assert retrieve(adventure, settings, StubEmbedder()) == {"used": [], "error": None}


def test_missing_embedding_model_is_reported(db, adventure, settings, bank):
    settings.embedding_model = ""
    db.commit()
    result = retrieve(adventure, settings, StubEmbedder())
    assert result["used"] == [] and "embedding model" in result["error"]


def test_update_stats_bumps_only_the_used(db, adventure, settings, bank):
    settings.memory_top_k = 1
    db.commit()
    retrieve(adventure, settings, StubEmbedder((1.0, 0.0, 0.0)), update_stats=True)
    db.commit()
    db.expire_all()

    assert db.get(models.Memory, bank["x"].id).use_count == 1
    assert db.get(models.Memory, bank["x"].id).last_used_at is not None
    assert db.get(models.Memory, bank["y"].id).use_count == 0


def test_dry_runs_do_not_bump_the_counters(db, adventure, settings, bank):
    """Insights assembles a context without spending a turn. It must not
    look like the memories were used."""
    retrieve(adventure, settings, StubEmbedder(), update_stats=False)
    db.commit()
    db.expire_all()
    assert all(db.get(models.Memory, m.id).use_count == 0 for m in bank.values())


# --------------------------------------------------------------- the egress

def memory_selects(statements):
    return [
        s for s in statements
        if "FROM memories" in s and s.lstrip().upper().startswith("SELECT")
    ]


def test_the_json_column_is_gone(db):
    """`memories.embedding` held the vectors before migration 38, and
    nothing read it afterward. Migration 42 dropped it. Restoring it would
    bring back 4 MB of dead weight and a second place vectors can be
    written from. That second place is how the model-switch bug happened
    (test_embedding_model_switch.py)."""
    columns = {c["name"] for c in sa_inspect(engine).get_columns("memories")}
    assert "embedding" not in columns
    assert {"embedding_blob", "embedded"} <= columns


def test_the_catalogue_query_carries_no_vectors(db, adventure, settings, bank, sql_log):
    """The query that decides which memories are in play must stay tiny.
    This is the query that used to pull the whole bank across the wire."""
    retrieve(adventure, settings, StubEmbedder())
    catalogue = [s for s in memory_selects(sql_log) if "memories.pinned" in s]
    assert catalogue, "expected a catalogue query"
    assert not any("embedding_blob" in s for s in catalogue)


def test_a_second_turn_reads_no_vectors_at_all(db, adventure, settings, bank, sql_log):
    """The point of the cache: back-to-back turns on one adventure pay for the
    vectors once."""
    retrieve(adventure, settings, StubEmbedder())
    sql_log.clear()
    retrieve(adventure, settings, StubEmbedder())
    assert not any("embedding_blob" in s for s in memory_selects(sql_log))


def test_only_the_new_memory_is_fetched_after_one_is_added(db, adventure, settings, bank, sql_log):
    """A growing bank must not re-read the vectors it already holds."""
    retrieve(adventure, settings, StubEmbedder())
    added = add_memory(db, adventure, "about w", (0.5, 0.5, 0.0))

    sql_log.clear()
    retrieve(adventure, settings, StubEmbedder())
    vector_reads = [s for s in memory_selects(sql_log) if "embedding_blob" in s]
    assert len(vector_reads) == 1
    # One placeholder means one id: the new memory, and nothing else.
    assert vector_reads[0].count("?") == 1
    assert added.id in {m["id"] for m in
                        retrieve(adventure, settings, StubEmbedder((0.5, 0.5, 0.0)))["used"]}


def test_only_top_k_texts_are_fetched(db, adventure, settings, bank, sql_log):
    settings.memory_top_k = 1
    db.commit()
    retrieve(adventure, settings, StubEmbedder())
    text_reads = [s for s in memory_selects(sql_log) if "memories.text" in s]
    assert len(text_reads) == 1
    assert text_reads[0].count("?") == 1


# ---------------------------------------------------------------- staleness

def test_a_rewritten_vector_is_not_served_from_cache(db, adventure, settings, bank):
    """The cache's one genuine hazard: a memory keeps its id while its
    vector changes, so an id-set check alone would continue serving the
    old vector. Editing a memory's text and re-embedding it does exactly
    that.
    """
    settings.memory_top_k = 1
    db.commit()
    first = retrieve(adventure, settings, StubEmbedder((0.0, 0.0, 1.0)))
    assert [m["id"] for m in first["used"]] == [bank["z"].id]

    # z is re-embedded onto the x axis, all within one gap between turns.
    memorybank.set_vector(bank["z"], [1.0, 0.0, 0.0])
    db.commit()

    again = retrieve(adventure, settings, StubEmbedder((0.0, 0.0, 1.0)))
    assert [m["id"] for m in again["used"]] != [bank["z"].id]


def test_a_deleted_memory_leaves_the_cache(db, adventure, settings, bank):
    retrieve(adventure, settings, StubEmbedder())
    db.delete(bank["x"])
    db.commit()

    used = retrieve(adventure, settings, StubEmbedder((1.0, 0.0, 0.0)))["used"]
    assert bank["x"].id not in {m["id"] for m in used}
    assert memorybank._vector_cache[adventure.id].keys() == {bank["y"].id, bank["z"].id}


def test_the_cache_is_bounded(db, adventure, settings, bank):
    """It holds vectors indefinitely, so without a bound a long-running process
    accumulates every adventure ever played."""
    retrieve(adventure, settings, StubEmbedder())
    for fake_id in range(1000, 1000 + memorybank.VECTOR_CACHE_ADVENTURES + 2):
        memorybank._vectors_for(db, fake_id, [])
    assert len(memorybank._vector_cache) == memorybank.VECTOR_CACHE_ADVENTURES
    assert adventure.id not in memorybank._vector_cache  # evicted, least recent


def test_forget_cached_vectors_drops_an_adventure(db, adventure, settings, bank):
    retrieve(adventure, settings, StubEmbedder())
    assert adventure.id in memorybank._vector_cache
    memorybank.forget_cached_vectors(adventure.id)
    assert adventure.id not in memorybank._vector_cache


# ----------------------------------------------------------------- eviction

def test_eviction_marks_the_least_recently_used(db, adventure, settings):
    settings.memory_bank_capacity = 1
    db.commit()
    now = models.utcnow()
    recent = add_memory(db, adventure, "recent", (1.0, 0.0, 0.0), use_count=1)
    stale = add_memory(db, adventure, "stale", (0.0, 1.0, 0.0), use_count=1)
    recent.last_used_at = now
    stale.last_used_at = now - timedelta(days=30)
    db.commit()

    memorybank._evict_over_capacity(adventure, settings, db)
    db.expire_all()

    assert db.get(models.Memory, stale.id).forgotten is True
    assert db.get(models.Memory, recent.id).forgotten is False


def test_eviction_breaks_ties_on_use_count(db, adventure, settings):
    """Two memories last used at the same moment: the one the story has
    used less is the one that goes. This must be only a tiebreak. Ranking
    on the count first is what used to freeze the bank (see below)."""
    settings.memory_bank_capacity = 1
    db.commit()
    now = models.utcnow()
    keep = add_memory(db, adventure, "used often", (1.0, 0.0, 0.0), use_count=5)
    drop = add_memory(db, adventure, "used once", (0.0, 1.0, 0.0), use_count=1)
    keep.last_used_at = drop.last_used_at = now
    db.commit()

    memorybank._evict_over_capacity(adventure, settings, db)
    db.expire_all()

    assert db.get(models.Memory, drop.id).forgotten is True
    assert db.get(models.Memory, keep.id).forgotten is False


def test_a_newborn_is_not_evicted_by_the_bank_it_joins(db, adventure, settings):
    """The bank used to stop accepting new memories. Eviction ranked on
    use_count first, and a memory written this turn has never been used.
    Once every existing memory had been retrieved even once, the newborn
    became the lowest-ranked row in the bank. Eviction then removed it in
    the same post-turn run that wrote it, before retrieval ever saw it.
    That state never recovers: counts only go up, so no memory written
    after it could get in either."""
    settings.memory_bank_capacity = 3
    db.commit()
    now = models.utcnow()
    established = [
        add_memory(db, adventure, f"used once {i}", (1.0, 0.0, 0.0),
                   use_count=1, last_used_at=now - timedelta(minutes=3 - i))
        for i in range(3)
    ]
    newborn = add_memory(db, adventure, "written this turn", (0.0, 1.0, 0.0))

    memorybank._evict_over_capacity(adventure, settings, db)
    db.expire_all()

    assert db.get(models.Memory, newborn.id).forgotten is False
    # the least recently used goes instead
    assert db.get(models.Memory, established[0].id).forgotten is True
    assert db.get(models.Memory, established[2].id).forgotten is False


def test_a_full_bank_still_turns_over(db, adventure, settings):
    """The same failure seen over several turns: a bank at capacity must
    keep accepting new memories, or the adventure stops remembering
    anything past the point where it filled up."""
    settings.memory_bank_capacity = 3
    now = models.utcnow()
    db.commit()
    for i in range(3):
        add_memory(db, adventure, f"opening {i}", (1.0, 0.0, 0.0),
                   use_count=1, last_used_at=now - timedelta(minutes=10 - i))

    later = []
    for turn in range(4):
        later.append(add_memory(db, adventure, f"turn {turn}", (0.0, 1.0, 0.0)))
        memorybank._evict_over_capacity(adventure, settings, db)
    db.expire_all()

    active = {m.text for m in db.query(models.Memory).filter(
        models.Memory.adventure_id == adventure.id,
        models.Memory.forgotten.is_(False),
    )}
    assert active == {"turn 1", "turn 2", "turn 3"}


def test_eviction_never_touches_a_pin(db, adventure, settings):
    """Capacity yields to pins: if everything active is pinned there is nothing
    to evict, and the bank is allowed to sit over capacity."""
    settings.memory_bank_capacity = 1
    db.commit()
    pins = [add_memory(db, adventure, f"pin {i}", (1.0, 0.0, 0.0), pinned=True)
            for i in range(3)]

    memorybank._evict_over_capacity(adventure, settings, db)
    db.expire_all()

    assert all(db.get(models.Memory, p.id).forgotten is False for p in pins)


def test_eviction_reads_no_vectors(db, adventure, settings, bank, sql_log):
    """It ran every turn and pulled the whole bank to count it."""
    settings.memory_bank_capacity = 1
    db.commit()
    sql_log.clear()
    memorybank._evict_over_capacity(adventure, settings, db)
    assert not any("embedding_blob" in s for s in memory_selects(sql_log))


def test_evicted_memories_drop_out_of_the_cache(db, adventure, settings, bank):
    retrieve(adventure, settings, StubEmbedder())
    settings.memory_bank_capacity = 1
    db.commit()
    memorybank._evict_over_capacity(adventure, settings, db)

    used = retrieve(adventure, settings, StubEmbedder())["used"]
    assert len(used) == 1
    assert len(memorybank._vector_cache[adventure.id]) == 1


# ------------------------------------------------------------- embed queue

def test_embed_pending_picks_only_unembedded_memories(db, adventure, settings):
    done = add_memory(db, adventure, "already done", (1.0, 0.0, 0.0))
    todo = add_memory(db, adventure, "needs a vector", None)
    add_memory(db, adventure, "evicted, skip", None, forgotten=True)

    embedder = StubEmbedder((0.0, 1.0, 0.0))
    memorybank.embedding_provider = lambda s: embedder
    asyncio.run(memorybank._embed_pending(adventure, settings, db))
    db.expire_all()

    assert db.get(models.Memory, todo.id).embedded is True
    assert embedder.calls == 1
    # The one already embedded keeps the vector it had.
    assert db.get(models.Memory, done.id).embedding_blob == memorybank.vectors.pack(
        [1.0, 0.0, 0.0]
    )


def test_embed_pending_reads_no_vectors(db, adventure, settings, bank, sql_log):
    add_memory(db, adventure, "needs a vector", None)
    embedder = StubEmbedder()
    memorybank.embedding_provider = lambda s: embedder

    sql_log.clear()
    asyncio.run(memorybank._embed_pending(adventure, settings, db))
    assert not any("embedding_blob" in s for s in memory_selects(sql_log))
