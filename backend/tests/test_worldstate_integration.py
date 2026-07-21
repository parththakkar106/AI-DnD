"""End-to-end HTTP test for RPG world state (Phase 12): a scenario with a
stat_schema, a turn whose (faked) AI reply carries a state delta block, and
undo rolling the world state back.

    python -m pytest tests/test_worldstate_integration.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import PromptParts
from app.routers import adventures

SCHEMA = {
    "player": {"hp": {"min": 0, "max": 100, "initial": 100, "max_delta_per_turn": 30}},
    "npcs": {
        "gwen": {
            "name": "Gwen", "keys": "Gwen",
            "desc": "A loyal ranger ally.",
            "stats": {"trust": {"min": -100, "max": 100, "initial": 0}},
        },
    },
    "flags": {"alarm": {"desc": "The enemy is alerted", "initial": False}},
    "milestones": {"win": {"desc": "Win the fight"}},
}

# The faked model narrates and appends a delta that exceeds the per-turn cap
# (so we can see the engine clamp it), flips a flag, and completes a milestone.
AI_REPLY = (
    "The goblin's blade bites deep and Gwen nods at your resolve.\n\n"
    '```state\n{"player.hp": -80, "npc.gwen.trust": 15, "flags.alarm": true, "milestones.win": true}\n```'
)


class FakeProvider:
    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        yield ("text", AI_REPLY)


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="rpg@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="Dungeon", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, scenario_id=scenario.id, title="Run",
        world_state=adventures.worldstate.instantiate(SCHEMA),
    )
    setup.add(adv)
    setup.flush()
    # "Gwen" in the story text makes her NPC in-scene (matches the "gwen" npc's keys).
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start",
                            text="You face a goblin. Gwen watches."))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    monkeypatch.setattr(adventures, "OpenAICompatibleProvider", FakeProvider)
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
        adventures._active_turns.clear()
        Base.metadata.drop_all(bind=engine)


def _world(adv_id):
    db = SessionLocal()
    try:
        return db.get(models.Adventure, adv_id).world_state
    finally:
        db.close()


def _last_ai_text(adv_id):
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return adv.actions[-1].text
    finally:
        db.close()


def _play(client, text="attack the goblin"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions", json={"type": "do", "text": text})
    assert r.status_code == 200, r.text
    return r


def test_turn_applies_clamped_delta_and_strips_block(client):
    _play(client)
    ws = _world(client.adv_id)
    assert ws["player"]["hp"] == 70          # -80 capped to -30
    assert ws["npc"]["gwen"]["trust"] == 15
    assert ws["flags"]["alarm"] is True
    assert ws["milestones"]["win"]["reached"] is True
    # The state block is not shown to the player.
    assert "```state" not in _last_ai_text(client.adv_id)
    assert "goblin's blade" in _last_ai_text(client.adv_id)
    # ...but the raw model reply (with the block) is kept for the Insights view.
    db = SessionLocal()
    try:
        snap = db.get(models.Adventure, client.adv_id).actions[-1].context_snapshot
    finally:
        db.close()
    assert "```state" in snap["raw_output"]
    assert '"player.hp": -80' in snap["raw_output"]


def test_action_world_changes_summary(client):
    _play(client)
    db = SessionLocal()
    try:
        changes = db.get(models.Adventure, client.adv_id).actions[-1].world_changes
    finally:
        db.close()
    by_label = {c["label"]: c for c in changes}
    assert by_label["hp"]["delta"] == -30           # clamped stat, signed delta
    assert by_label["gwen trust"]["delta"] == 15     # npc.<id>.<stat> -> "id stat"
    assert by_label["alarm"] == {"kind": "flag", "label": "alarm", "on": True}
    assert by_label["win"]["kind"] == "milestone"


def test_world_state_endpoint(client):
    _play(client)
    r = client.get(f"/api/adventures/{client.adv_id}/world-state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema"]["player"]["hp"]["max"] == 100
    assert body["state"]["player"]["hp"] == 70


def test_override_world_state_endpoint(client):
    r = client.put(f"/api/adventures/{client.adv_id}/world-state",
                    json={"player.hp": 5, "flags.alarm": True, "npc.bogus.trust": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"]["player"]["hp"] == 5
    assert body["state"]["flags"]["alarm"] is True
    assert body["report"]["rejected"][0]["reason"] == "unknown npc"
    # persisted to the DB, not just the response.
    assert _world(client.adv_id)["player"]["hp"] == 5

    # bypasses max_delta_per_turn (30) — a direct correction, not a turn.
    r = client.put(f"/api/adventures/{client.adv_id}/world-state", json={"player.hp": 100})
    assert r.json()["state"]["player"]["hp"] == 100


def test_undo_reverts_world_state(client):
    _play(client)
    assert _world(client.adv_id)["player"]["hp"] == 70
    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 200, r.text
    assert _world(client.adv_id)["player"]["hp"] == 100  # back to initial
    assert _world(client.adv_id)["milestones"] == {}


def test_retry_does_not_double_apply(client):
    _play(client)
    assert _world(client.adv_id)["player"]["hp"] == 70
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text
    assert _world(client.adv_id)["player"]["hp"] == 70  # not 40
