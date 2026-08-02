"""End-to-end HTTP tests for retry history: every attempt at an AI turn is kept
as a variant of the same action, browsable and (for the last message)
switchable, restoring the world/script state that attempt produced.

    python -m pytest tests/test_retry_variants.py -v
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
from app.providers import PromptParts, ProviderError
from app.routers import adventures

SCHEMA = {"player": {"hp": {"min": 0, "max": 100, "initial": 100}}}

# Each turn spends 10 gold, so a double-applied or un-rolled-back attempt shows.
GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


class ScriptedProvider:
    """Streams the next canned reply each call, so successive retries differ."""
    replies: list = []
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        index = min(ScriptedProvider.calls, len(ScriptedProvider.replies) - 1)
        ScriptedProvider.calls += 1
        reply = ScriptedProvider.replies[index]
        if isinstance(reply, Exception):
            raise reply
        yield ("text", reply)


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="variants@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="S", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, title="Cave", scenario_id=scenario.id,
        script_state={}, world_state={"player": {"hp": 100}},
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start", text="You enter a cave."))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = ["Attempt one."]
    ScriptedProvider.calls = 0
    monkeypatch.setattr(adventures, "OpenAICompatibleProvider", ScriptedProvider)
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


def _adv(adv_id):
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return adv.script_state, adv.world_state
    finally:
        db.close()


def _actions(client):
    return client.get(f"/api/adventures/{client.adv_id}").json()["actions"]


def _play(client, text="look around"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": "do", "text": text})
    assert r.status_code == 200, r.text


def _retry(client):
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text
    return r


# ---------------------------------------------------------------- keeping them

def test_retry_keeps_the_discarded_attempt(client):
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    assert _actions(client)[-1]["text"] == "Attempt one."

    _retry(client)
    actions = _actions(client)
    # One AI action still, not two — the retry replaced the live text in place.
    assert [a["type"] for a in actions] == ["start", "do", "ai"]
    last = actions[-1]
    assert last["text"] == "Attempt two."
    assert last["variant_count"] == 2
    assert last["variant_index"] == 1

    r = client.get(f"/api/adventures/{client.adv_id}/actions/{last['id']}/variants")
    assert r.status_code == 200, r.text
    assert [v["text"] for v in r.json()] == ["Attempt one.", "Attempt two."]
    assert [v["active"] for v in r.json()] == [False, True]


def test_never_retried_action_has_no_variants(client):
    _play(client)
    last = _actions(client)[-1]
    assert last["variant_count"] == 0
    assert client.get(
        f"/api/adventures/{client.adv_id}/actions/{last['id']}/variants").json() == []


def test_three_attempts_all_kept_in_order(client):
    ScriptedProvider.replies = ["One.", "Two.", "Three."]
    _play(client)
    _retry(client)
    _retry(client)
    last = _actions(client)[-1]
    assert last["variant_count"] == 3
    assert last["variant_index"] == 2
    variants = client.get(
        f"/api/adventures/{client.adv_id}/actions/{last['id']}/variants").json()
    assert [v["text"] for v in variants] == ["One.", "Two.", "Three."]


# ---------------------------------------------------------------- switching

def test_switching_back_restores_that_attempt_state(client):
    ScriptedProvider.replies = [
        "You take a scratch.\n```state\n{\"player.hp\": -5}\n```",
        "You take a beating.\n```state\n{\"player.hp\": -40}\n```",
    ]
    _play(client)
    assert _adv(client.adv_id)[1]["player"]["hp"] == 95
    _retry(client)
    script_state, world_state = _adv(client.adv_id)
    assert world_state["player"]["hp"] == 60
    assert script_state == {"gold": 10}  # rolled back, not stacked to 20

    last = _actions(client)[-1]
    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{last['id']}/variant", json={"index": 0})
    assert r.status_code == 200, r.text
    assert r.json()["text"].startswith("You take a scratch")
    assert r.json()["variant_index"] == 0
    # The stats follow the narration back.
    script_state, world_state = _adv(client.adv_id)
    assert world_state["player"]["hp"] == 95
    assert script_state == {"gold": 10}

    # And forward again.
    client.post(f"/api/adventures/{client.adv_id}/actions/{last['id']}/variant",
                json={"index": 1})
    assert _adv(client.adv_id)[1]["player"]["hp"] == 60


def test_switching_updates_the_world_change_chips(client):
    ScriptedProvider.replies = [
        "A scratch.\n```state\n{\"player.hp\": -5}\n```",
        "A beating.\n```state\n{\"player.hp\": -40}\n```",
    ]
    _play(client)
    _retry(client)
    last = _actions(client)[-1]
    assert last["world_changes"][0]["delta"] == -40

    client.post(f"/api/adventures/{client.adv_id}/actions/{last['id']}/variant",
                json={"index": 0})
    assert _actions(client)[-1]["world_changes"][0]["delta"] == -5


def test_cannot_switch_a_turn_the_story_moved_past(client):
    ScriptedProvider.replies = ["One.", "Two.", "Three."]
    _play(client)
    _retry(client)
    retried = _actions(client)[-1]
    _play(client)  # story continues from "Two."

    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{retried['id']}/variant", json={"index": 0})
    assert r.status_code == 400
    assert "latest message" in r.json()["detail"]
    # Still readable, though — that's the whole point of keeping them.
    variants = client.get(
        f"/api/adventures/{client.adv_id}/actions/{retried['id']}/variants").json()
    assert [v["text"] for v in variants] == ["One.", "Two."]


def test_switching_to_a_missing_index_is_rejected(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    last = _actions(client)[-1]
    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{last['id']}/variant", json={"index": 7})
    assert r.status_code == 400


# ---------------------------------------------------------------- edge cases

def test_failed_retry_leaves_the_previous_attempt_in_charge(client):
    """A provider error mid-retry must undo the rollback, or the stats on
    screen would silently disagree with the text still shown."""
    ScriptedProvider.replies = ["Attempt one.", ProviderError("upstream is down")]
    _play(client)
    assert _adv(client.adv_id)[0] == {"gold": 10}

    client.post(f"/api/adventures/{client.adv_id}/retry")

    actions = _actions(client)
    assert actions[-1]["text"] == "Attempt one."  # text never lost
    assert _adv(client.adv_id)[0] == {"gold": 10}  # and the state still matches it


def test_undo_removes_the_action_and_its_history(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 200, r.text
    assert [a["type"] for a in r.json()] == ["start"]
    assert _adv(client.adv_id)[0] == {}


def test_editing_the_text_updates_the_live_variant(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    last = _actions(client)[-1]
    client.patch(f"/api/adventures/{client.adv_id}/actions/{last['id']}",
                 json={"text": "Two, but better."})

    # Page away and back: the edit must survive, not be reverted by the switch.
    client.post(f"/api/adventures/{client.adv_id}/actions/{last['id']}/variant",
                json={"index": 0})
    client.post(f"/api/adventures/{client.adv_id}/actions/{last['id']}/variant",
                json={"index": 1})
    assert _actions(client)[-1]["text"] == "Two, but better."


def test_retry_keeps_the_turn_index(client):
    """A retry re-runs the same turn, so the world-state clock (which drives
    cooldowns) must not advance."""
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    before = _actions(client)[-1]["index"]
    _retry(client)
    assert _actions(client)[-1]["index"] == before


def test_export_and_import_round_trips_variants(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    bundle = client.get(f"/api/adventures/{client.adv_id}/export").json()
    ai = [a for a in bundle["actions"] if a["type"] == "ai"][0]
    assert [v["text"] for v in ai["variants"]] == ["One.", "Two."]
    assert ai["variantIndex"] == 1

    r = client.post("/api/adventures/import", json=bundle)
    assert r.status_code == 201, r.text
    imported = r.json()["id"]
    actions = client.get(f"/api/adventures/{imported}").json()["actions"]
    assert actions[-1]["variant_count"] == 2
    assert actions[-1]["variant_index"] == 1


def test_import_clamps_an_out_of_range_variant_index(client):
    bundle = {
        "format": "ai-dnd-adventure-v1", "title": "Hand-edited",
        "actions": [{
            "index": 0, "type": "ai", "text": "Only.",
            "variants": [{"text": "Only."}], "variantIndex": 9,
        }],
    }
    r = client.post("/api/adventures/import", json=bundle)
    assert r.status_code == 201, r.text
    actions = client.get(f"/api/adventures/{r.json()['id']}").json()["actions"]
    assert actions[0]["variant_index"] == 0
