"""Tests for undo/retry rolling back the shared script_state scoreboard
(plan/11-state-revert-and-retry-fix.md).

Run from the backend dir:  python -m pytest tests/test_state_revert.py -v
"""
import os
import tempfile

# Point the app at a throwaway SQLite file BEFORE importing anything that binds
# the engine at import time (app.database reads AIDND_DB_PATH on import).
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest
from fastapi import HTTPException

from app import memorybank, models
from app.database import Base, SessionLocal, engine
from app.routers import adventures


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        adventures._active_turns.clear()


def _make_adventure(db, script_state):
    user = models.User(is_guest=False)
    db.add(user)
    db.flush()
    adv = models.Adventure(user_id=user.id, title="T", script_state=script_state)
    db.add(adv)
    db.flush()
    return user, adv


def _add(db, adv, index, type_, text="x", state_before=None):
    a = models.Action(
        adventure_id=adv.id, index=index, type=type_, text=text,
        state_before=state_before,
    )
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------- undo

def test_undo_reverts_state_to_before_the_turn(db):
    # A turn took the scoreboard from {gold:0} -> {gold:10}. The player action
    # carries the pre-turn snapshot; current state is the mutated one.
    user, adv = _make_adventure(db, {"gold": 10})
    _add(db, adv, 0, "start", state_before=None)
    _add(db, adv, 1, "do", state_before={"gold": 0})
    _add(db, adv, 2, "ai", state_before={"gold": 0})
    db.commit()

    adventures.undo_turn(adv.id, db=db, user=user)

    assert adv.script_state == {"gold": 0}
    assert [a.type for a in adv.actions] == ["start"]


def test_undo_of_bare_continue_uses_ai_snapshot(db):
    # A "continue" turn has no player action; the AI action's own snapshot is
    # the pre-turn state.
    user, adv = _make_adventure(db, {"gold": 5})
    _add(db, adv, 0, "start")
    _add(db, adv, 1, "ai", state_before={"gold": 0})
    db.commit()

    adventures.undo_turn(adv.id, db=db, user=user)

    assert adv.script_state == {"gold": 0}
    assert [a.type for a in adv.actions] == ["start"]


def test_undo_leaves_state_untouched_when_snapshot_missing(db):
    # Pre-migration actions have state_before = NULL: don't clobber the state.
    user, adv = _make_adventure(db, {"gold": 10})
    _add(db, adv, 0, "start")
    _add(db, adv, 1, "do", state_before=None)
    _add(db, adv, 2, "ai", state_before=None)
    db.commit()

    adventures.undo_turn(adv.id, db=db, user=user)

    assert adv.script_state == {"gold": 10}


def test_undo_raises_when_nothing_to_undo(db):
    user, adv = _make_adventure(db, {})
    _add(db, adv, 0, "start")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        adventures.undo_turn(adv.id, db=db, user=user)
    assert exc.value.status_code == 400


def test_undo_blocked_by_active_turn_lock(db):
    user, adv = _make_adventure(db, {})
    _add(db, adv, 0, "start")
    _add(db, adv, 1, "ai", state_before={})
    db.commit()

    adventures.acquire_turn_lock(adv.id)  # a turn is "generating"
    try:
        with pytest.raises(HTTPException) as exc:
            adventures.undo_turn(adv.id, db=db, user=user)
        assert exc.value.status_code == 409
        # The failed undo must not have released someone else's lock.
        assert adv.id in adventures._active_turns
    finally:
        adventures._active_turns.discard(adv.id)


def test_undo_prunes_memory_covering_removed_actions(db):
    user, adv = _make_adventure(db, {})
    for i in range(4):
        _add(db, adv, i, "ai" if i % 2 else "do", state_before={})
    # A memory summarizing actions up to index 3, which undo will delete.
    covering = models.Memory(adventure_id=adv.id, text="m", source_start=0, source_end=3)
    keep = models.Memory(adventure_id=adv.id, text="k", source_start=0, source_end=1)
    db.add_all([covering, keep])
    db.commit()

    adventures.undo_turn(adv.id, db=db, user=user)  # removes indexes 2 & 3

    texts = {m.text for m in adv.memories}
    assert texts == {"k"}


# ---------------------------------------------------------------- prune helper

def test_prune_dangling_memories_counts_and_removes(db):
    user, adv = _make_adventure(db, {})
    _add(db, adv, 0, "do")
    _add(db, adv, 1, "ai")
    db.add_all([
        models.Memory(adventure_id=adv.id, text="live", source_start=0, source_end=1),
        models.Memory(adventure_id=adv.id, text="dead", source_start=2, source_end=5),
    ])
    db.commit()

    removed = memorybank.prune_dangling_memories(adv, db)
    db.commit()
    db.refresh(adv)  # expire_on_commit=False: reload the memories collection

    assert removed == 1
    assert {m.text for m in adv.memories} == {"live"}


# ---------------------------------------------------------------- snapshot

def test_snapshot_state_is_an_independent_deep_copy(db):
    _, adv = _make_adventure(db, {"nested": {"n": 1}})
    snap = adventures.snapshot_state(adv)
    adv.script_state["nested"]["n"] = 99
    assert snap == {"nested": {"n": 1}}  # unaffected by later mutation


def test_snapshot_state_handles_non_dict(db):
    _, adv = _make_adventure(db, {})
    adv.script_state = None
    assert adventures.snapshot_state(adv) == {}


# ---------------------------------------------------------------- retry

def test_retry_restores_state_before_regenerating(db, monkeypatch):
    # Retry deletes the last AI action and must roll the scoreboard back to that
    # action's snapshot so regeneration doesn't stack output mutations.
    user, adv = _make_adventure(db, {"gold": 20})  # 20 = double-applied bug value
    _add(db, adv, 0, "start")
    _add(db, adv, 1, "do", state_before={"gold": 0})
    _add(db, adv, 2, "ai", state_before={"gold": 10})
    db.commit()

    monkeypatch.setattr(adventures.limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(adventures, "check_demo_cap", lambda *a, **k: None)

    async def _noop(*a, **k):
        if False:
            yield  # make it an async generator
    monkeypatch.setattr(adventures, "generate_turn", _noop)

    adventures.retry_action(adv.id, request=None, db=db, user=user)

    assert adv.script_state == {"gold": 10}
    assert [a.type for a in adv.actions] == ["start", "do"]
    adventures._active_turns.discard(adv.id)
