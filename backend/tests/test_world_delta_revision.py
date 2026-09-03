"""Editing the newest turn's state changes, and the referee they go back through.

The AI proposes a delta each turn. This is the endpoint that lets a player say
the proposal was wrong — the hit was smaller than that, the trust was not earned,
the turn missed a change — and have the turn replayed with their numbers.

The point of the tests below is that a revision is not an override. It is the
same turn, played again from the same starting state, through the same referee:
the per-turn cap still caps it, an unknown path is still refused, and the story
below the turn is untouched because only the newest turn can be revised at all.

    python -m pytest tests/test_world_delta_revision.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models, worldstate
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers import adventures

from fakes import ScriptedProvider

SCHEMA = {
    "player": {"hp": {"min": 0, "max": 100, "initial": 100, "max_delta_per_turn": 30}},
    "npcs": {
        "gwen": {
            "name": "Gwen", "keys": "Gwen",
            "stats": {"trust": {"min": -100, "max": 100, "initial": 0}},
        },
    },
    "flags": {"alarm": {"initial": False}},
    "milestones": {"win": {"desc": "Win the fight"}},
}

# Two turns, so a test can revise the newest one and check that the older one is
# refused. The first spends 20 hp; the second another 20, leaving 60.
REPLIES = [
    "A blade grazes you. Gwen watches.\n"
    '```state_delta\n{"player.hp": -20, "npc.gwen.trust": 5}\n```',
    'It bites again.\n```state_delta\n{"player.hp": -20}\n```',
]


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="delta@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="Dungeon", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, scenario_id=scenario.id, title="Run",
        world_state=worldstate.instantiate(SCHEMA),
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, type="start",
                            text="You face a goblin. Gwen watches."))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = list(REPLIES)
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


def _play(client, text="attack"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": "do", "text": text})
    assert r.status_code == 200, r.text


def _ai_actions(adv_id):
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return [a.id for a in adv.actions if a.type == "ai"]
    finally:
        db.close()


def _world(adv_id):
    db = SessionLocal()
    try:
        return db.get(models.Adventure, adv_id).world_state
    finally:
        db.close()


def _revise(client, action_id, delta):
    return client.put(
        f"/api/adventures/{client.adv_id}/actions/{action_id}/world-delta", json=delta
    )


def test_reads_back_the_delta_the_turn_proposed(client):
    _play(client)
    action_id = _ai_actions(client.adv_id)[-1]
    r = client.get(f"/api/adventures/{client.adv_id}/actions/{action_id}/world-delta")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delta"] == {"player.hp": -20, "npc.gwen.trust": 5}
    assert body["editable"] is True
    assert body["revised"] is False


def test_revision_replays_the_turn_from_where_it_started(client):
    """The delta is replaced, not stacked on what the AI's already did."""
    _play(client)
    action_id = _ai_actions(client.adv_id)[-1]
    assert _world(client.adv_id)["player"]["hp"] == 80

    r = _revise(client, action_id, {"player.hp": -5})
    assert r.status_code == 200, r.text
    # 100 - 5, not 80 - 5: the turn was rewound before the new delta was applied.
    assert r.json()["state"]["player"]["hp"] == 95
    assert _world(client.adv_id)["player"]["hp"] == 95
    # A path left out of the object is a change the turn no longer makes.
    assert _world(client.adv_id)["npc"]["gwen"]["trust"] == 0


def test_the_node_carries_the_revised_outcome(client):
    """Undo and take-switching restore a node's outcome, so it has to be the
    revised one. Leaving the AI's there would put its numbers back."""
    _play(client)
    action_id = _ai_actions(client.adv_id)[-1]
    _revise(client, action_id, {"player.hp": -5})

    db = SessionLocal()
    try:
        action = db.get(models.Action, action_id)
        assert action.world_state_after["player"]["hp"] == 95
        assert action.world_delta["delta"] == {"player.hp": -5}
        assert action.world_delta["revised"] is True
        assert action.world_changes_revised is True
        # The chips are rendered from the same column, so they move with it.
        assert [c["delta"] for c in action.world_changes] == [-5]
    finally:
        db.close()


def test_a_revision_is_held_to_the_same_limits(client):
    """`max_delta_per_turn` is 30. A revision is a turn's delta, not an
    override, so a bigger change is clamped and reported the same way."""
    _play(client)
    action_id = _ai_actions(client.adv_id)[-1]
    r = _revise(client, action_id, {"player.hp": -90})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["player"]["hp"] == 70          # -90 capped to -30
    assert r.json()["report"]["clamped"][0]["path"] == "player.hp"


def test_an_unknown_path_is_refused_and_the_rest_applies(client):
    _play(client)
    action_id = _ai_actions(client.adv_id)[-1]
    r = _revise(client, action_id, {"player.hp": -5, "npc.bogus.trust": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"]["player"]["hp"] == 95
    assert body["report"]["rejected"][0]["reason"] == "unknown npc"


def test_a_change_can_be_added_to_a_turn(client):
    _play(client)
    action_id = _ai_actions(client.adv_id)[-1]
    r = _revise(client, action_id,
                {"player.hp": -20, "flags.alarm": True, "milestones.win": True})
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert state["flags"]["alarm"] is True
    assert state["milestones"]["win"]["reached"] is True


def test_only_the_newest_turn_can_be_revised(client):
    """An older turn is the starting position of every turn under it."""
    _play(client)
    _play(client, "again")
    first, second = _ai_actions(client.adv_id)
    assert _world(client.adv_id)["player"]["hp"] == 60

    r = _revise(client, first, {"player.hp": -1})
    assert r.status_code == 409, r.text
    assert _world(client.adv_id)["player"]["hp"] == 60

    r = client.get(f"/api/adventures/{client.adv_id}/actions/{first}/world-delta")
    assert r.json()["editable"] is False
    # The newest one is still revisable, and it replays from what the first left
    # behind rather than from the start of the story.
    r = _revise(client, second, {"player.hp": -10})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["player"]["hp"] == 70


def test_a_players_turn_has_no_delta_to_revise(client):
    _play(client)
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, client.adv_id)
        player_action = next(a.id for a in adv.actions if a.type == "do")
    finally:
        db.close()
    assert _revise(client, player_action, {"player.hp": -1}).status_code == 400


def test_an_empty_revision_takes_the_whole_turn_back(client):
    _play(client)
    action_id = _ai_actions(client.adv_id)[-1]
    r = _revise(client, action_id, {})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["player"]["hp"] == 100
    assert _world(client.adv_id)["npc"]["gwen"]["trust"] == 0
