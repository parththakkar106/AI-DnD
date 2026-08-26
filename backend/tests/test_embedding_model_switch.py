"""Switching embedding models must re-embed the bank.

Vectors from two different models are not comparable. They live in
different spaces and often have different widths. Changing the model must
discard the stored vectors and let the post-turn pass rebuild them.

This worked while the vectors lived in `memories.embedding`. The settings
route nulled that column, and the embed queue picked up the rows. Migration
38 moved the vectors to `embedding_blob` and added an `embedded` flag beside
them, but the bulk clear kept nulling only the old column. The blob
survived, the flag stayed true, and `_embed_pending` (which filters on
`embedded IS FALSE`) never saw the rows. The bank kept ranking against the
previous model's vectors.

Nothing reports this failure. `cosine` returns 0.0 on a width mismatch, so a
different-width model scores every memory zero, and retrieval silently
returns whichever rows sort first. A same-width model scores plausible
garbage instead.

    python -m pytest tests/test_embedding_model_switch.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import asyncio

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, memorybank, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app

DIMS = 8


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    memorybank._vector_cache.clear()
    setup = SessionLocal()
    user = models.User(is_guest=False, email="switch@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(
        user_id=user.id, api_key="enc:dummy", model="test-model",
        embedding_model="model-a",
    ))
    adventure = models.Adventure(
        user_id=user.id, title="Cave", script_state={}, memory_bank_enabled=True
    )
    setup.add(adventure)
    setup.flush()
    for i in range(5):
        memory = models.Memory(adventure_id=adventure.id, text=f"Memory {i}")
        memorybank.set_vector(memory, [float(i)] + [0.0] * (DIMS - 1))
        setup.add(memory)
    setup.commit()
    adv_id, user_id = adventure.id, user.id
    setup.close()

    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    c.adv_id = adv_id
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        memorybank._vector_cache.clear()
        Base.metadata.drop_all(bind=engine)


def memories(db):
    return db.query(models.Memory).order_by(models.Memory.id).all()


def test_the_bank_starts_embedded(client):
    db = SessionLocal()
    try:
        rows = memories(db)
        assert len(rows) == 5
        assert all(m.embedded for m in rows)
        assert all(m.embedding_blob for m in rows)
    finally:
        db.close()


def test_changing_the_model_clears_every_vector(client):
    r = client.put("/api/settings", json={"embedding_model": "model-b"})
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        rows = memories(db)
        assert [m.embedding_blob for m in rows] == [None] * 5, \
            "the blob survived the model change"
        assert not any(m.embedded for m in rows), \
            "`embedded` stayed true, so nothing will ever re-embed these"
    finally:
        db.close()


def test_cleared_memories_are_queued_for_re_embedding(client):
    """The `embedded` flag is not cosmetic. It is the only condition
    `_embed_pending` filters on, so this test confirms the bank actually
    recovers."""
    client.put("/api/settings", json={"embedding_model": "model-b"})

    db = SessionLocal()
    try:
        pending = (
            db.query(models.Memory)
            .filter(models.Memory.embedded.is_(False),
                    models.Memory.forgotten.is_(False))
            .all()
        )
        assert len(pending) == 5
    finally:
        db.close()


def test_retrieval_uses_no_stale_vector_after_the_switch(client, monkeypatch):
    """Until the re-embed runs, the bank must return nothing rather than
    ranking against the old model's vectors."""
    client.put("/api/settings", json={"embedding_model": "model-b"})

    class Embedder:
        async def embed(self, texts):
            return [[1.0] + [0.0] * (DIMS - 1) for _ in texts]

    monkeypatch.setattr(memorybank, "embedding_provider", lambda s: Embedder())

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        settings = db.query(models.Settings).first()
        result = asyncio.run(
            memorybank.retrieve_memories(adventure, settings, update_stats=False)
        )
        assert result["used"] == []
    finally:
        db.close()


def test_an_unrelated_settings_change_keeps_the_vectors(client):
    """Only an embedding-model change may clear the bank. Re-embedding costs
    an API call per memory."""
    r = client.put("/api/settings", json={"model": "some-other-chat-model"})
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        rows = memories(db)
        assert all(m.embedded for m in rows)
        assert all(m.embedding_blob for m in rows)
    finally:
        db.close()
