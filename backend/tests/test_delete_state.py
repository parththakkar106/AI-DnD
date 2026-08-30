"""Deleting a turn puts the shared state back.

`script_state` and `world_state` belong to the adventure, not to the node
that changed them. Undo, retry, a take and a branch switch all restore them;
the delete endpoint did not. Deleting an AI turn removed the text and left
everything the turn did to the numbers standing.

The visible symptom was the cooldown clock. It lives in
`world_state._meta.last_changed` and it holds a depth. A deleted turn left
its depth there, and the turn played in its place is played at that same
depth, so the referee refused the change as one that had happened this very
turn — on a turn the story no longer contains. Delete the AI reply because
you did not like the stat change it proposed, press Continue, and the same
change comes back refused as "changed too recently".

    python -m pytest tests/test_delete_state.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers import adventures
from fakes import ScriptedProvider

# `mana` carries a cooldown, so a clock that was not rolled back shows up as
# a refusal rather than as a number that is merely off.
SCHEMA = {
    "player": {
        "hp": {"min": 0, "max": 100, "initial": 100},
        "mana": {"min": 0, "max": 50, "initial": 50, "cooldown": 2},
    }
}

# Ten gold a turn. A total that only ever climbs makes a missing rollback
# obvious: it is off by exactly one turn's worth.
GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""

DRAIN = 'Drained.\n```state\n{"player.mana": -10}\n```'


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="delstate@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="S", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, title="Tower", scenario_id=scenario.id,
        script_state={}, world_state={"player": {"hp": 100, "mana": 50}},
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, type="start", text="You begin."))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = [DRAIN]
    ScriptedProvider.calls = 0
    monkeypatch.setattr(adventures.turns, "OpenAICompatibleProvider", ScriptedProvider)
    monkeypatch.setattr(auth, "resolve_provider_config", lambda s: auth.ProviderConfig(
        "http://fake", "k", "test-model", False))
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
        adventures.turns._active_turns.clear()
        Base.metadata.drop_all(bind=engine)


# ------------------------------------------------------------------ helpers

def _play(client, text="look around"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": "do", "text": text})
    assert r.status_code == 200, r.text


def _continue(client):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": "continue", "text": ""})
    assert r.status_code == 200, r.text


def _delete(client, action_id):
    return client.delete(f"/api/adventures/{client.adv_id}/actions/{action_id}")


def _state(adv_id):
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return adv.script_state, adv.world_state
    finally:
        db.close()


def _ai_rows(adv_id):
    db = SessionLocal()
    try:
        return (
            db.query(models.Action)
            .filter(models.Action.adventure_id == adv_id, models.Action.type == "ai")
            .order_by(models.Action.depth, models.Action.id)
            .all()
        )
    finally:
        db.close()


def _last_changes(adv_id):
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return adv.actions[-1].world_changes
    finally:
        db.close()


# ------------------------------------------------------- the reported bug

def test_deleting_the_ai_turn_rewinds_the_world_state(client):
    _play(client)
    assert _state(client.adv_id)[1]["player"]["mana"] == 40

    _delete(client, _ai_rows(client.adv_id)[-1].id)

    _, world = _state(client.adv_id)
    assert world["player"]["mana"] == 50, "the drain went with the turn"
    assert not (world.get("_meta") or {}).get("last_changed"), "and so did its clock"


def test_the_next_turn_is_not_refused_for_a_deleted_turn_s_cooldown(client):
    """The bug as a player meets it: delete the reply, press Continue, and
    the change it proposes is refused as one that already happened."""
    _play(client)
    _delete(client, _ai_rows(client.adv_id)[-1].id)

    _continue(client)

    assert _state(client.adv_id)[1]["player"]["mana"] == 40, "the drain lands"
    assert [c for c in _last_changes(client.adv_id) if c["kind"] == "rejected"] == []


def test_deleting_the_ai_turn_rewinds_the_script_state(client):
    """The same restore, on the other half of the shared state. Without it a
    replayed turn stacks its script run on top of the deleted one's."""
    _play(client)
    assert _state(client.adv_id)[0] == {"gold": 10}

    _delete(client, _ai_rows(client.adv_id)[-1].id)
    assert _state(client.adv_id)[0] == {}

    _continue(client)
    assert _state(client.adv_id)[0] == {"gold": 10}, "one turn of gold, not two"


# ------------------------------------------------- deleting further back

def test_deleting_a_turn_the_story_moved_past_leaves_the_tip_alone(client):
    """A restore reads the tip's own outcome, not the deleted node's
    neighbour, so removing a turn from the middle of the story does not roll
    the numbers back to that point. The text goes; the state stays."""
    _play(client)
    _play(client, "press on")
    before = _state(client.adv_id)
    assert before[0] == {"gold": 20}

    first_ai = _ai_rows(client.adv_id)[0]
    assert _delete(client, first_ai.id).status_code == 204

    assert _state(client.adv_id) == before


def test_delete_is_blocked_while_a_turn_is_generating(client):
    """The endpoint writes the shared state now, so it takes the same lock
    undo takes rather than racing the turn that is about to write it."""
    _play(client)
    action_id = _ai_rows(client.adv_id)[-1].id

    adventures.turns.acquire_turn_lock(client.adv_id)  # a turn is "generating"
    try:
        assert _delete(client, action_id).status_code == 409
        # The refused delete must not have released someone else's lock.
        assert client.adv_id in adventures.turns._active_turns
    finally:
        adventures.turns._active_turns.discard(client.adv_id)
    assert len(_ai_rows(client.adv_id)) == 1, "and the turn is still there"
