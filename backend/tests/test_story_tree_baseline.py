"""Phase 14 SP0: the regression contract for the story tree.

This file exists to answer one question repeatedly, as the storage model is
replaced underneath the app: do existing adventures still behave exactly as
they did?

It drives the product the way a player does, over HTTP, asserting only on
API responses. It never reaches into the ORM to check how something is
stored. Everything it asserts is true of the linear implementation today and
must stay true of the tree, because a linear story is a tree with one branch.

    python -m pytest tests/test_story_tree_baseline.py -v

This file must pass unmodified through SP1 (schema), SP2 (branch clause),
and SP3 (memories on nodes). If a change here looks necessary in one of
those subphases, the change is wrong, not the test. SP4 is the first
subphase allowed to move it, and only for the variant-count semantics
called out in plan/14.
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers import adventures

from fakes import ScriptedProvider

# A world-state schema, so the RPG layer is exercised rather than skipped.
SCHEMA = {"player": {"hp": {"min": 0, "max": 100, "initial": 100}}}

# Ten gold a turn. A turn that double-applies its hooks, or one that fails to
# roll back on retry, shows up here as a wrong total rather than as nothing.
GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""

OPENING = "You enter a cave."


def _make_world(monkeypatch, *, seeded_actions: int = 0):
    """Create a user, a scenario, and an adventure, with `seeded_actions`
    extra story actions written straight to the database. Paging tests need
    more turns than it is practical to play one at a time."""
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="baseline@example.com")
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
    setup.add(models.Action(adventure_id=adv.id, type="start", text=OPENING))
    for i in range(seeded_actions):
        setup.add(models.Action(
            adventure_id=adv.id,
            type="ai" if i % 2 else "do",
            text=f"Seeded turn {i}.",
        ))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = ["A reply."]
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
    return c


@pytest.fixture()
def client(monkeypatch):
    c = _make_world(monkeypatch)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        adventures.turns._active_turns.clear()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def long_client(monkeypatch):
    """An adventure longer than one window, so paging is real."""
    c = _make_world(monkeypatch, seeded_actions=adventures.ACTION_PAGE + 10)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        adventures.turns._active_turns.clear()
        Base.metadata.drop_all(bind=engine)


# ------------------------------------------------------------------ helpers

def _open(client):
    r = client.get(f"/api/adventures/{client.adv_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _actions(client):
    return _open(client)["actions"]


def _texts(client):
    return [a["text"] for a in _actions(client)]


def _play(client, text="look around", type="do"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": type, "text": text})
    assert r.status_code == 200, r.text
    return r


def _state(adv_id):
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return adv.script_state, adv.world_state
    finally:
        db.close()


# ------------------------------------------------------- opening the story

def test_adventure_opens_on_a_window_with_a_total(long_client):
    """The payload includes the newest window and the whole story's length.
    That is how the client knows more actions exist above it."""
    payload = _open(long_client)
    seeded = adventures.ACTION_PAGE + 11  # + the start action
    assert payload["action_count"] == seeded
    assert len(payload["actions"]) == adventures.ACTION_PAGE
    # The newest window, so it ends at the newest action.
    assert payload["actions"][-1]["text"] == f"Seeded turn {seeded - 2}."


def test_short_adventure_returns_everything(client):
    payload = _open(client)
    assert payload["action_count"] == 1
    assert [a["text"] for a in payload["actions"]] == [OPENING]


# --------------------------------------------------------------- the turn

def test_a_turn_appends_the_player_action_then_the_ai_action(client):
    ScriptedProvider.replies = ["The dark presses in."]
    _play(client, "light a torch")
    actions = _actions(client)
    assert [a["type"] for a in actions] == ["start", "do", "ai"]
    assert actions[1]["text"] == "> You light a torch."
    assert actions[2]["text"] == "The dark presses in."


def test_say_and_story_and_continue_all_work(client):
    ScriptedProvider.replies = ["One.", "Two.", "Three."]
    _play(client, "hello", type="say")
    _play(client, "The wind rises.", type="story")
    _play(client, "", type="continue")
    types = [a["type"] for a in _actions(client)]
    assert types == ["start", "say", "ai", "story", "ai", "ai"]


def test_the_story_so_far_is_replayed_into_the_prompt(client):
    ScriptedProvider.replies = ["First.", "Second."]
    _play(client, "go north")
    _play(client, "go south")
    story = ScriptedProvider.prompts[-1][1]
    assert OPENING in story
    assert "First." in story
    assert "go north" in story


def test_scripts_run_once_per_turn(client):
    """The gold script adds ten a turn. Two turns is twenty — not forty."""
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _play(client)
    script_state, _ = _state(client.adv_id)
    assert script_state["gold"] == 20


# ------------------------------------------------------------------ retry

def test_retry_replaces_the_text_and_keeps_the_attempt(client):
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    assert _texts(client)[-1] == "Attempt one."

    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text

    actions = _actions(client)
    # This turn still has one AI action. A retry creates a new take, not a new turn.
    assert [a["type"] for a in actions] == ["start", "do", "ai"]
    assert actions[-1]["text"] == "Attempt two."

    r = client.get(
        f"/api/adventures/{client.adv_id}/actions/{actions[-1]['id']}/variants")
    assert r.status_code == 200, r.text
    assert [v["text"] for v in r.json()] == ["Attempt one.", "Attempt two."]
    assert [v["active"] for v in r.json()] == [False, True]


def test_retry_does_not_stack_script_effects(client):
    """The discarded attempt's ten gold is rolled back, so one turn plus one
    retry is still ten, not twenty."""
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    client.post(f"/api/adventures/{client.adv_id}/retry")
    script_state, _ = _state(client.adv_id)
    assert script_state["gold"] == 10


def test_switching_back_to_an_earlier_attempt_restores_it(client):
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    client.post(f"/api/adventures/{client.adv_id}/retry")
    action_id = _actions(client)[-1]["id"]

    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{action_id}/variant",
        json={"index": 0})
    assert r.status_code == 200, r.text
    assert _texts(client)[-1] == "Attempt one."
    # The script state that attempt produced comes back with it.
    script_state, _ = _state(client.adv_id)
    assert script_state["gold"] == 10


def test_only_the_newest_turn_can_be_switched(client):
    """An older turn's alternatives stay readable but not selectable. The
    story after it continues from what is live."""
    ScriptedProvider.replies = ["One.", "Again.", "Two."]
    _play(client)
    client.post(f"/api/adventures/{client.adv_id}/retry")
    older_id = _actions(client)[-1]["id"]
    _play(client)  # the story moves on

    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{older_id}/variant",
        json={"index": 0})
    assert r.status_code == 400


# ------------------------------------------------------------------- undo

def test_undo_removes_the_whole_turn_and_rolls_state_back(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client, "go north")
    _play(client, "go south")
    assert len(_actions(client)) == 5

    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 200, r.text
    page = r.json()
    # Both the AI action and the player action that prompted it are gone.
    assert [a["type"] for a in page["actions"]] == ["start", "do", "ai"]
    assert page["total"] == 3
    # The second turn's ten gold is also gone.
    script_state, _ = _state(client.adv_id)
    assert script_state["gold"] == 10


def test_undo_returns_a_window_not_the_whole_story(long_client):
    before = _open(long_client)["action_count"]
    r = long_client.post(f"/api/adventures/{long_client.adv_id}/undo")
    assert r.status_code == 200, r.text
    page = r.json()
    assert len(page["actions"]) == adventures.ACTION_PAGE
    # The seeded story ends on an `ai` preceded by a `do`, so a turn is two
    # actions and undo takes both.
    assert page["total"] == before - 2
    assert page["has_more"] is True


def test_the_opening_cannot_be_undone(client):
    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 400


# ------------------------------------------------------------------ paging

def test_paging_walks_back_to_the_start_without_gaps_or_repeats(long_client):
    """The property that matters: paging all the way up must show every
    action exactly once, in order. A tree must preserve this invariant. The
    anchor is always an action, never a position."""
    payload = _open(long_client)
    total = payload["action_count"]
    seen = [a["id"] for a in payload["actions"]]

    has_more = len(seen) < total
    while has_more:
        r = long_client.get(
            f"/api/adventures/{long_client.adv_id}/actions",
            params={"before_id": seen[0]})
        assert r.status_code == 200, r.text
        page = r.json()
        ids = [a["id"] for a in page["actions"]]
        assert ids, "a page claiming more must return some"
        seen = ids + seen
        has_more = page["has_more"]

    assert len(seen) == total
    assert len(set(seen)) == total, "an action was served twice"
    assert seen == sorted(seen), "pages came back out of order"


def test_a_page_is_bounded_by_the_requested_limit(long_client):
    r = long_client.get(f"/api/adventures/{long_client.adv_id}/actions",
                        params={"limit": 5})
    assert r.status_code == 200, r.text
    page = r.json()
    assert len(page["actions"]) == 5
    assert page["has_more"] is True
    assert page["total"] == adventures.ACTION_PAGE + 11


def test_paging_past_the_start_reports_the_end(long_client):
    r = long_client.get(f"/api/adventures/{long_client.adv_id}/actions",
                        params={"limit": 5})
    oldest = r.json()["actions"][0]["id"]
    # Continue paging until it reaches the oldest action.
    seen_all = False
    cursor = oldest
    for _ in range(100):
        page = long_client.get(
            f"/api/adventures/{long_client.adv_id}/actions",
            params={"before_id": cursor, "limit": 20}).json()
        if not page["has_more"]:
            seen_all = True
            break
        cursor = page["actions"][0]["id"]
    assert seen_all


# ------------------------------------------------------- editing and deleting

def test_editing_an_action_sticks(client):
    ScriptedProvider.replies = ["Original."]
    _play(client)
    action_id = _actions(client)[-1]["id"]
    r = client.patch(f"/api/adventures/{client.adv_id}/actions/{action_id}",
                     json={"text": "Edited."})
    assert r.status_code == 200, r.text
    # Re-read from the database instead of trusting the response. An edit
    # that only appears in the response has already reverted for anyone who
    # reloads.
    assert _texts(client)[-1] == "Edited."


def test_editing_a_retried_action_survives_a_reload(client):
    """The edit has to reach the live attempt too, or paging away and back
    reverts it."""
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    client.post(f"/api/adventures/{client.adv_id}/retry")
    action_id = _actions(client)[-1]["id"]
    client.patch(f"/api/adventures/{client.adv_id}/actions/{action_id}",
                 json={"text": "Edited."})
    assert _texts(client)[-1] == "Edited."


def test_deleting_an_action_removes_it(client):
    ScriptedProvider.replies = ["One."]
    _play(client)
    action_id = _actions(client)[-1]["id"]
    r = client.delete(f"/api/adventures/{client.adv_id}/actions/{action_id}")
    assert r.status_code == 204, r.text
    assert len(_actions(client)) == 2


# ---------------------------------------------------------------- memories

def test_memories_are_created_listed_and_deleted(client):
    r = client.post(f"/api/adventures/{client.adv_id}/memories",
                    json={"text": "The torch went out."})
    assert r.status_code == 201, r.text
    memory_id = r.json()["id"]

    listed = client.get(f"/api/adventures/{client.adv_id}/memories").json()
    assert [m["text"] for m in listed] == ["The torch went out."]

    r = client.patch(
        f"/api/adventures/{client.adv_id}/memories/{memory_id}",
        json={"pinned": True})
    assert r.status_code == 200, r.text
    assert r.json()["pinned"] is True

    r = client.delete(f"/api/adventures/{client.adv_id}/memories/{memory_id}")
    assert r.status_code == 204, r.text
    assert client.get(f"/api/adventures/{client.adv_id}/memories").json() == []


# ------------------------------------------------------------------ export

def test_export_carries_the_whole_story(client):
    """A bundle is a backup: it contains every action, not just the window.

    SP6 changed the format assertion below, as this note said it would. It
    also changed one more line than the note allowed for: the `variants`
    array in `test_export_keeps_retry_attempts`. That change reflects the
    same fact: a bundle that stores coordinates has no use for a repeating
    group. Everything else here still passes unmodified.
    """
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client, "go north")
    _play(client, "go south")

    r = client.get(f"/api/adventures/{client.adv_id}/export")
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["format"] == "ai-dnd-adventure-v2"
    assert bundle["title"] == "Cave"
    assert [a["text"] for a in bundle["actions"]] == [
        OPENING, "> You go north.", "One.", "> You go south.", "Two.",
    ]


def test_export_round_trips_through_import(client):
    ScriptedProvider.replies = ["One."]
    _play(client, "go north")
    bundle = client.get(f"/api/adventures/{client.adv_id}/export").json()

    r = client.post("/api/adventures/import", json=bundle)
    assert r.status_code == 201, r.text
    imported = r.json()
    assert imported["id"] != client.adv_id
    assert imported["title"] == "Cave"
    assert [a["text"] for a in imported["actions"]] == [
        OPENING, "> You go north.", "One.",
    ]


def test_export_keeps_retry_attempts(client):
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    client.post(f"/api/adventures/{client.adv_id}/retry")

    bundle = client.get(f"/api/adventures/{client.adv_id}/export").json()
    ai = [a for a in bundle["actions"] if a["type"] == "ai"]
    assert [a["text"] for a in ai] == ["Attempt one.", "Attempt two."]
    assert [a["live"] for a in ai] == [False, True]


# -------------------------------------------------------------- world state

def test_world_state_is_readable_and_survives_a_turn(client):
    ScriptedProvider.replies = ["Nothing changes."]
    _play(client)
    r = client.get(f"/api/adventures/{client.adv_id}/world-state")
    assert r.status_code == 200, r.text
    assert r.json()["state"]["player"]["hp"] == 100
