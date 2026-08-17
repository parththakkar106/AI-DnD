"""Tests for undo/retry rolling back the shared script_state scoreboard
(plan/11-state-revert-and-retry-fix.md).

Phase 14 SP4 turned the snapshots around. An action used to carry the state as
it stood *before* it ran, and rolling back read the snapshot off the action
being removed. It carries what it left *behind* now, and rolling back reads it
off the node in front — which is the same number arrived at from the other
side, and the only version a retry can use: attempts at one turn share a
starting position and differ precisely in their outcome.

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

from app import attempts, memorybank, models
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


def _add(db, adv, index, type_, text="x", state_after=None):
    a = models.Action(
        adventure_id=adv.id, index=index, type=type_, text=text,
        state_after=state_after,
    )
    db.add(a)
    db.flush()
    return a


def _forget_snapshots(db, adv):
    """Blank every outcome, the way a row written before SP4 looks.

    Straight SQL, because `tree.stamp_outcome` runs on every flush precisely so
    that a node written through the ORM cannot end up without one.
    """
    db.query(models.Action).filter_by(adventure_id=adv.id).update(
        {"state_after": None, "world_state_after": None}, synchronize_session=False
    )
    db.commit()
    db.expire_all()


# ---------------------------------------------------------------- undo

def test_undo_reverts_state_to_before_the_turn(db):
    # A turn took the scoreboard from {gold:0} -> {gold:10}. The node in front
    # of the turn is what says where it started; current state is the mutated
    # one.
    user, adv = _make_adventure(db, {"gold": 10})
    _add(db, adv, 0, "start", state_after={"gold": 0})
    _add(db, adv, 1, "do", state_after={"gold": 0})
    _add(db, adv, 2, "ai", state_after={"gold": 10})
    db.commit()

    adventures.undo_turn(adv.id, db=db, user=user)

    assert adv.script_state == {"gold": 0}
    assert [a.type for a in adv.actions] == ["start"]


def test_undo_of_bare_continue_uses_the_node_in_front(db):
    # A "continue" turn has no player action, so the opening is what the story
    # falls back to.
    user, adv = _make_adventure(db, {"gold": 5})
    _add(db, adv, 0, "start", state_after={"gold": 0})
    _add(db, adv, 1, "ai", state_after={"gold": 5})
    db.commit()

    adventures.undo_turn(adv.id, db=db, user=user)

    assert adv.script_state == {"gold": 0}
    assert [a.type for a in adv.actions] == ["start"]


def test_undo_leaves_state_untouched_when_snapshot_missing(db):
    # A row the SP4 migration could not derive an outcome for: leave the live
    # state alone rather than resetting it to nothing.
    user, adv = _make_adventure(db, {"gold": 10})
    _add(db, adv, 0, "start")
    _add(db, adv, 1, "do")
    _add(db, adv, 2, "ai")
    _forget_snapshots(db, adv)

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
    _add(db, adv, 1, "ai", state_after={})
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
        _add(db, adv, i, "ai" if i % 2 else "do", state_after={})
    # A memory summarizing actions up to index 3, which undo will delete.
    covering = models.Memory(adventure_id=adv.id, text="m", source_start=0, source_end=3)
    keep = models.Memory(adventure_id=adv.id, text="k", source_start=0, source_end=1)
    db.add_all([covering, keep])
    db.commit()

    adventures.undo_turn(adv.id, db=db, user=user)  # removes indexes 2 & 3

    texts = {m.text for m in adv.memories}
    assert texts == {"k"}


# -------------------------------------------------------- withdrawing a node

def test_forget_node_withdraws_only_what_that_node_produced(db):
    """Phase 14 SP3: a memory hangs off the node its block ends on, so removing
    a node is a lookup rather than a scan for memories that have fallen off the
    end of the story."""
    user, adv = _make_adventure(db, {})
    _add(db, adv, 0, "do")
    second = _add(db, adv, 1, "ai")
    db.add_all([
        models.Memory(adventure_id=adv.id, text="hangs off node 1",
                      source_start=0, source_end=1),
        models.Memory(adventure_id=adv.id, text="hangs off node 0",
                      source_start=0, source_end=0),
    ])
    db.commit()

    removed = memorybank.forget_node(db, adv, second)
    db.commit()
    db.refresh(adv)  # expire_on_commit=False: reload the memories collection

    assert removed == 1
    assert {m.text for m in adv.memories} == {"hangs off node 0"}


# ---------------------------------------------------------------- snapshot

def test_snapshot_outcome_is_an_independent_deep_copy(db):
    _, adv = _make_adventure(db, {"nested": {"n": 1}})
    node = models.Action(adventure_id=adv.id, index=0, type="ai", text="x")
    attempts.snapshot_outcome(adv, node)
    adv.script_state["nested"]["n"] = 99
    assert node.state_after == {"nested": {"n": 1}}  # unaffected by later mutation


def test_snapshot_outcome_handles_non_dict(db):
    _, adv = _make_adventure(db, {})
    adv.script_state = None
    node = models.Action(adventure_id=adv.id, index=0, type="ai", text="x")
    attempts.snapshot_outcome(adv, node)
    assert node.state_after == {}


def test_restore_state_ignores_a_node_with_no_outcome(db):
    _, adv = _make_adventure(db, {"gold": 7})
    attempts.restore_state(adv, models.Action(adventure_id=adv.id, index=0, type="ai"))
    assert adv.script_state == {"gold": 7}
    attempts.restore_state(adv, None)
    assert adv.script_state == {"gold": 7}


# ---------------------------------------------------------------- retry

def test_retry_restores_the_state_the_turn_started_from(db, monkeypatch):
    # Retry must roll the scoreboard back to what the node in front of the AI
    # action left behind, so regeneration doesn't stack output mutations on top
    # of the attempt being replaced.
    user, adv = _make_adventure(db, {"gold": 20})  # 20 = double-applied bug value
    _add(db, adv, 0, "start", state_after={"gold": 0})
    _add(db, adv, 1, "do", state_after={"gold": 10})
    _add(db, adv, 2, "ai", state_after={"gold": 20})
    db.commit()

    monkeypatch.setattr(adventures.limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(adventures, "check_demo_cap", lambda *a, **k: None)

    async def _noop(*a, **k):
        if False:
            yield  # make it an async generator
    monkeypatch.setattr(adventures, "generate_turn", _noop)

    adventures.retry_action(adv.id, request=None, db=db, user=user)

    assert adv.script_state == {"gold": 10}
    # Nothing is written until a replacement actually arrives: the attempt on
    # screen is left exactly as it was, and stays the live one.
    assert [a.type for a in adv.actions] == ["start", "do", "ai"]
    last = adv.actions[-1]
    assert last.live is True
    assert last.variant_count == 0
    assert last.state_after == {"gold": 20}  # its own outcome, untouched
    adventures._active_turns.discard(adv.id)
