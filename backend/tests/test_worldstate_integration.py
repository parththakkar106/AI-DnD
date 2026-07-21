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
    "npc": {"trust": {"min": -100, "max": 100, "initial": 0}},
    "flags": {"alarm": {"desc": "The enemy is alerted", "initial": False}},
    "milestones": {"win": {"desc": "Win the fight"}},
    "npc_card_types": ["character"],
}

# The faked model narrates and appends a delta that exceeds the per-turn cap
# (so we can see the engine clamp it), flips a flag, and completes a milestone.
AI_REPLY = (
    "The goblin's blade bites deep and Gwen nods at your resolve.\n\n"
    '```state\n{"player.hp": -80, "npc.9.trust": 15, "flags.alarm": true, "milestones.win": true}\n```'
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
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start",
                            text="You face a goblin. Gwen watches."))
    # NPC story card so "Gwen" is in scene (matches npc.9 in the delta).
    setup.add(models.StoryCard(adventure_id=adv.id, id=9, type="character",
                               name="Gwen", keys="Gwen", entry="A loyal ranger."))
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
    assert ws["npc"]["9"]["trust"] == 15
    assert ws["flags"]["alarm"] is True
    assert ws["milestones"]["win"]["reached"] is True
    # The state block is not shown to the player.
    assert "```state" not in _last_ai_text(client.adv_id)
    assert "goblin's blade" in _last_ai_text(client.adv_id)


def test_world_state_endpoint(client):
    _play(client)
    r = client.get(f"/api/adventures/{client.adv_id}/world-state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema"]["player"]["hp"]["max"] == 100
    assert body["state"]["player"]["hp"] == 70


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
