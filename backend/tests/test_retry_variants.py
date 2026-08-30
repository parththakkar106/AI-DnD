"""End-to-end HTTP tests for retry history: every attempt at an AI turn is kept
as a variant of the same action, browsable and (for the last message)
switchable, restoring the world/script state that attempt produced.

    python -m pytest tests/test_retry_variants.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import ProviderError
from app.routers import adventures

from fakes import ScriptedProvider

SCHEMA = {"player": {"hp": {"min": 0, "max": 100, "initial": 100}}}

# Each turn spends 10 gold, so a double-applied or un-rolled-back attempt shows.
GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


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
    setup.add(models.Action(adventure_id=adv.id, type="start", text="You enter a cave."))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = ["Attempt one."]
    ScriptedProvider.calls = 0
    ScriptedProvider.prompts = []
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
    # One AI action still, not two. The retry replaced the live text in place.
    assert [a["type"] for a in actions] == ["start", "do", "ai"]
    last = actions[-1]
    assert last["text"] == "Attempt two."
    assert last["take_count"] == 2
    assert last["take_index"] == 1

    r = client.get(f"/api/adventures/{client.adv_id}/actions/{last['id']}/variants")
    assert r.status_code == 200, r.text
    assert [v["text"] for v in r.json()] == ["Attempt one.", "Attempt two."]
    assert [v["active"] for v in r.json()] == [False, True]


def test_retry_context_excludes_the_attempt_being_replaced(client):
    """A retry produces a fresh take on the same turn. The row survives the
    retry because it holds the variant history, so it is still in
    `adventure.actions` while the replacement context is assembled. The
    context builder must filter it out, or the model continues past the
    attempt it is replacing and writes a sequel that blends both."""
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    _retry(client)

    retry_story = ScriptedProvider.prompts[-1][1]
    assert "Attempt one." not in retry_story
    # The turn's own player action must still be there. It is what the model responds to.
    assert "look around" in retry_story
    assert "You enter a cave." in retry_story


def test_retry_context_keeps_earlier_ai_turns(client):
    """Only the action being retried is dropped, not AI history in general."""
    ScriptedProvider.replies = ["First turn.", "Second turn.", "Second, again."]
    _play(client, "go north")
    _play(client, "go south")
    _retry(client)

    retry_story = ScriptedProvider.prompts[-1][1]
    assert "First turn." in retry_story
    assert "Second turn." not in retry_story


def test_never_retried_action_has_no_variants(client):
    _play(client)
    last = _actions(client)[-1]
    assert last["take_count"] == 1  # itself, and nothing to page to
    assert client.get(
        f"/api/adventures/{client.adv_id}/actions/{last['id']}/variants").json() == []


def test_three_attempts_all_kept_in_order(client):
    ScriptedProvider.replies = ["One.", "Two.", "Three."]
    _play(client)
    _retry(client)
    _retry(client)
    last = _actions(client)[-1]
    assert last["take_count"] == 3
    assert last["take_index"] == 2
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
    assert r.json()["take_index"] == 0
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
    # The variant is still readable. Keeping every attempt browsable is why it still exists.
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
    # Undo returns the newest window now, not the whole story.
    assert [a["type"] for a in r.json()["actions"]] == ["start"]
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


def _live_depth(client) -> int:
    """Returns the depth of the newest action the story tells."""
    db = SessionLocal()
    try:
        return (
            db.query(models.Action.depth)
            .filter(models.Action.adventure_id == client.adv_id,
                    models.Action.live.is_(True))
            .order_by(models.Action.depth.desc())
            .first()[0]
        )
    finally:
        db.close()


def test_retry_keeps_the_turn_depth(client):
    """A retry re-runs the same turn, so the world-state clock (which drives
    cooldowns) must not advance.

    The depth is read from the row rather than the payload. It is a coordinate
    on one branch, and the client pages by id, so it is not exposed.
    """
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    before = _live_depth(client)
    _retry(client)
    assert _live_depth(client) == before


def test_export_and_import_round_trips_variants(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    bundle = client.get(f"/api/adventures/{client.adv_id}/export").json()
    # SP6: the attempts are nodes in the bundle too, sharing one coordinate,
    # and `live` says which of them the story tells. The `variants` array
    # survives only in the v1 reader. See the hand-edited bundle below.
    ai = [a for a in bundle["actions"] if a["type"] == "ai"]
    assert [(a["text"], a["live"]) for a in ai] == [("One.", False), ("Two.", True)]
    assert len({(a["branch"], a["depth"]) for a in ai}) == 1

    r = client.post("/api/adventures/import", json=bundle)
    assert r.status_code == 201, r.text
    imported = r.json()["id"]
    actions = client.get(f"/api/adventures/{imported}").json()["actions"]
    assert [a["type"] for a in actions] == ["start", "do", "ai"]
    assert actions[-1]["text"] == "Two."

    # The pager reads 1/1 on the copy, because the import writes no
    # `parent_id` and `annotate_takes` groups on it. The attempts are both
    # there, at one coordinate, and `GET .../variants` still lists them. This
    # is a gap in the import rather than in the drop: `take_count` has been the
    # only number the client reads since SP9, and the import has never set the
    # column it is derived from.
    assert actions[-1]["take_count"] == 1
    variants = client.get(
        f"/api/adventures/{imported}/actions/{actions[-1]['id']}/variants").json()
    assert [v["text"] for v in variants] == ["One.", "Two."]


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
    assert actions[0]["take_index"] == 0
