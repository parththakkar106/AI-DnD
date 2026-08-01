"""Tests for "Update from scenario" — pulling an edited scenario's plot text,
story cards and stat schema back down over a running adventure's copy.

    python -m pytest tests/test_scenario_refresh.py -v
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

from app import auth, limits, models, worldstate
from app.database import Base, SessionLocal, engine, get_db
from app.main import app

SCHEMA = {
    "player": {"hp": {"min": 0, "max": 100, "initial": 100}},
    "npcs": {
        "gwen": {"name": "Gwen", "keys": "Gwen, ranger", "desc": "A loyal ranger.",
                 "stats": {"trust": {"min": -100, "max": 100, "initial": 0}}},
    },
    "flags": {"alarm": {"desc": "Alerted", "initial": False}},
    "milestones": {"win": {"desc": "Win the fight"}},
}


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="refresh@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    setup.commit()
    user_id = user.id
    setup.close()

    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    c.user_id = user_id
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def make_scenario(client, **kwargs):
    db = SessionLocal()
    try:
        cards = kwargs.pop("cards", [])
        scenario = models.Scenario(user_id=client.user_id, **kwargs)
        db.add(scenario)
        db.flush()
        for card in cards:
            db.add(models.StoryCard(scenario_id=scenario.id, **card))
        db.commit()
        return scenario.id
    finally:
        db.close()


def edit_scenario(scenario_id, cards=None, **fields):
    """Author-side edit, straight to the DB (the scenario API is tested elsewhere).

    `cards` is the scenario's full card list afterwards. Cards are matched to
    existing rows by name and edited in place, exactly as ScenarioEditor does
    (PATCH /story-cards/{id}) — card ids are stable across authoring, which is
    what source_ref tracking relies on.
    """
    db = SessionLocal()
    try:
        scenario = db.get(models.Scenario, scenario_id)
        for field, value in fields.items():
            setattr(scenario, field, value)
        if cards is not None:
            existing = {c.name: c for c in scenario.story_cards}
            for spec in cards:
                card = existing.pop(spec["name"], None)
                if card is None:
                    db.add(models.StoryCard(scenario_id=scenario_id, **spec))
                else:
                    for field, value in spec.items():
                        setattr(card, field, value)
            for card in existing.values():
                db.delete(card)
        db.commit()
    finally:
        db.close()


def start(client, scenario_id, placeholders=None):
    r = client.post("/api/adventures", json={
        "scenario_id": scenario_id, "placeholders": placeholders or {}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def get_adventure(client, adv_id):
    r = client.get(f"/api/adventures/{adv_id}")
    assert r.status_code == 200, r.text
    return r.json()


def preview(client, adv_id):
    r = client.get(f"/api/adventures/{adv_id}/refresh")
    assert r.status_code == 200, r.text
    return r.json()


def refresh(client, adv_id, placeholders=None):
    r = client.post(f"/api/adventures/{adv_id}/refresh",
                    json={"placeholders": placeholders or {}})
    assert r.status_code == 200, r.text
    return r.json()


def card_named(adventure, name):
    return next((c for c in adventure["story_cards"] if c["name"] == name), None)


# --------------------------------------------------------------------------- #
# Plot text
# --------------------------------------------------------------------------- #

def test_refresh_overwrites_plot_text_including_player_edits(client):
    sid = make_scenario(client, title="Keep", memory="Old memory",
                        authors_note="Old note", ai_instructions="Old rules")
    adv_id = start(client, sid)

    # The player tunes the adventure's copy, then the author edits the scenario.
    client.patch(f"/api/adventures/{adv_id}", json={"memory": "Player's own memory"})
    edit_scenario(sid, memory="New memory", authors_note="New note")

    plan = preview(client, adv_id)
    assert plan["has_changes"] is True
    assert plan["fields"]["memory"] == {"old": "Player's own memory", "new": "New memory"}
    assert "ai_instructions" not in plan["fields"]  # unchanged fields aren't listed

    adventure = refresh(client, adv_id)
    assert adventure["memory"] == "New memory"
    assert adventure["authors_note"] == "New note"
    assert adventure["ai_instructions"] == "Old rules"


def test_refresh_leaves_the_opening_action_and_title_alone(client):
    sid = make_scenario(client, title="Keep", prompt="You stand at the gate.")
    adv_id = start(client, sid)
    client.patch(f"/api/adventures/{adv_id}", json={"title": "My run"})
    edit_scenario(sid, title="Fortress", prompt="You stand in the throne room.")

    adventure = refresh(client, adv_id)
    # The story is built on the opening beat, and it is baked into memories and
    # the summary, so a refresh must never rewrite it.
    assert adventure["actions"][0]["text"] == "You stand at the gate."
    assert adventure["title"] == "My run"


def test_no_changes_reported_when_nothing_was_edited(client):
    sid = make_scenario(client, title="Keep", memory="Same", stat_schema=SCHEMA,
                        cards=[{"name": "Gate", "keys": "gate", "entry": "Iron."}])
    adv_id = start(client, sid)
    assert preview(client, adv_id)["has_changes"] is False


# --------------------------------------------------------------------------- #
# Story cards
# --------------------------------------------------------------------------- #

def test_cards_added_updated_and_removed_but_player_cards_survive(client):
    sid = make_scenario(client, title="Keep", cards=[
        {"name": "Gate", "keys": "gate", "entry": "An iron gate."},
        {"name": "Well", "keys": "well", "entry": "A dry well."},
    ])
    adv_id = start(client, sid)

    # The player writes their own card mid-play.
    client.post("/api/story-cards", json={
        "adventure_id": adv_id, "name": "My horse", "keys": "horse", "entry": "Bess."})

    # The author rewrites the Gate, drops the Well, adds a Tower.
    edit_scenario(sid, cards=[
        {"name": "Gate", "keys": "gate, portcullis", "entry": "A rusted iron gate."},
        {"name": "Tower", "keys": "tower", "entry": "It leans."},
    ])

    plan = preview(client, adv_id)
    assert plan["cards"] == {"added": ["Tower"], "updated": ["Gate"], "removed": ["Well"]}

    adventure = refresh(client, adv_id)
    names = sorted(c["name"] for c in adventure["story_cards"])
    assert names == ["Gate", "My horse", "Tower"]
    assert card_named(adventure, "Gate")["entry"] == "A rusted iron gate."
    # A player-authored card is never touched, even though the scenario has no
    # matching card — only scenario-derived copies are managed.
    assert card_named(adventure, "My horse")["entry"] == "Bess."


def test_a_renamed_scenario_card_is_updated_not_duplicated(client):
    sid = make_scenario(client, title="Keep",
                        cards=[{"name": "Gate", "keys": "gate", "entry": "Iron."}])
    adv_id = start(client, sid)
    db = SessionLocal()
    try:
        card = db.query(models.StoryCard).filter_by(scenario_id=sid).one()
        card.name = "Portcullis"
        db.commit()
    finally:
        db.close()

    adventure = refresh(client, adv_id)
    # Tracked by source_ref, so a rename is a rename — not a delete plus an add.
    assert [c["name"] for c in adventure["story_cards"]] == ["Portcullis"]


def test_legacy_cards_with_no_source_ref_are_matched_by_name_and_adopted(client):
    sid = make_scenario(client, title="Keep",
                        cards=[{"name": "Gate", "keys": "gate", "entry": "Iron."}])
    adv_id = start(client, sid)
    # Simulate an adventure created before source_ref existed.
    db = SessionLocal()
    try:
        for card in db.query(models.StoryCard).filter_by(adventure_id=adv_id):
            card.source_ref = None
        db.commit()
    finally:
        db.close()

    edit_scenario(sid, cards=[{"name": "Gate", "keys": "gate", "entry": "Rusted iron."}])
    adventure = refresh(client, adv_id)
    assert len(adventure["story_cards"]) == 1
    assert card_named(adventure, "Gate")["entry"] == "Rusted iron."

    db = SessionLocal()
    try:
        adopted = db.query(models.StoryCard).filter_by(adventure_id=adv_id).one()
        assert adopted.source_ref is not None  # syncs by id from here on
    finally:
        db.close()


def test_npc_cards_are_created_for_npcs_added_after_the_adventure_started(client):
    sid = make_scenario(client, title="Keep", stat_schema={"player": SCHEMA["player"]})
    adv_id = start(client, sid)
    assert get_adventure(client, adv_id)["story_cards"] == []

    edit_scenario(sid, stat_schema=SCHEMA)
    adventure = refresh(client, adv_id)
    gwen = card_named(adventure, "Gwen")
    assert gwen is not None and gwen["keys"] == "Gwen, ranger"


# --------------------------------------------------------------------------- #
# World state
# --------------------------------------------------------------------------- #

def test_refresh_keeps_live_values_but_syncs_the_shape(client):
    sid = make_scenario(client, title="Keep", stat_schema=SCHEMA)
    adv_id = start(client, sid)
    # Play happens: damage taken, a milestone reached.
    client.put(f"/api/adventures/{adv_id}/world-state",
               json={"player.hp": 40, "npc.gwen.trust": 25, "milestones.win": True})

    edited = {
        "player": {"hp": SCHEMA["player"]["hp"], "gold": {"min": 0, "initial": 10}},
        "npcs": {"gwen": {"name": "Gwen", "keys": "Gwen, ranger", "desc": "A loyal ranger.",
                          "stats": {"trust": {"min": -100, "max": 100, "initial": 0}}}},
        "flags": {},                       # the alarm flag is gone
        "milestones": SCHEMA["milestones"],
    }
    edit_scenario(sid, stat_schema=edited)

    plan = preview(client, adv_id)
    assert plan["world_state"] == {"added": ["player.gold"], "removed": ["flags.alarm"]}

    refresh(client, adv_id)
    r = client.get(f"/api/adventures/{adv_id}/world-state")
    state = r.json()["state"]
    # Values the schema still defines survive — a refresh is not a reset.
    assert state["player"]["hp"] == 40
    assert state["npc"]["gwen"]["trust"] == 25
    assert state["milestones"]["win"]["reached"] is True
    assert state["player"]["gold"] == 10       # new stat, at its initial
    assert state["flags"] == {}                # dropped stat, cleaned up


def test_reconcile_drops_a_removed_stats_cooldown_bookkeeping():
    state = worldstate.instantiate(SCHEMA)
    state["_meta"]["last_changed"] = {"player.hp": 3, "npc.gwen.trust": 4}
    reduced = {"player": SCHEMA["player"], "milestones": SCHEMA["milestones"]}

    new_state, report = worldstate.reconcile(state, reduced)
    assert "npc.gwen" in report["removed"]
    assert new_state["_meta"]["last_changed"] == {"player.hp": 3}


def test_reconcile_clears_everything_when_the_rpg_layer_is_removed():
    new_state, report = worldstate.reconcile(worldstate.instantiate(SCHEMA), None)
    assert new_state == {}
    assert report["removed"] == ["(all world state)"]


# --------------------------------------------------------------------------- #
# Placeholders
# --------------------------------------------------------------------------- #

def test_stored_placeholder_answers_are_reused_on_refresh(client):
    sid = make_scenario(client, title="Keep", memory="Your name is ${Hero}.",
                        cards=[{"name": "Sword", "keys": "sword",
                                "entry": "${Hero}'s blade."}])
    adv_id = start(client, sid, {"Hero": "Wren"})
    assert get_adventure(client, adv_id)["memory"] == "Your name is Wren."

    assert preview(client, adv_id)["placeholders_needed"] == []
    edit_scenario(sid, memory="${Hero}, you are late.")
    adventure = refresh(client, adv_id)
    # The answer is reused rather than re-injecting a literal ${Hero}.
    assert adventure["memory"] == "Wren, you are late."
    assert card_named(adventure, "Sword")["entry"] == "Wren's blade."


def test_missing_placeholder_answers_are_requested_then_remembered(client):
    sid = make_scenario(client, title="Keep", memory="Your name is ${Hero}.")
    adv_id = start(client, sid, {"Hero": "Wren"})
    # An adventure predating the stored-answers column.
    db = SessionLocal()
    try:
        db.get(models.Adventure, adv_id).placeholders = None
        db.commit()
    finally:
        db.close()

    assert preview(client, adv_id)["placeholders_needed"] == ["Hero"]
    adventure = refresh(client, adv_id, {"Hero": "Wren"})
    assert adventure["memory"] == "Your name is Wren."
    # Saved, so the player is only asked once.
    assert preview(client, adv_id)["placeholders_needed"] == []


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #

def test_refresh_is_unavailable_once_the_scenario_is_gone(client):
    sid = make_scenario(client, title="Keep", memory="Old")
    adv_id = start(client, sid)
    db = SessionLocal()
    try:
        db.delete(db.get(models.Scenario, sid))
        db.commit()
    finally:
        db.close()

    assert client.get(f"/api/adventures/{adv_id}/refresh").status_code == 404
    assert client.post(f"/api/adventures/{adv_id}/refresh", json={}).status_code == 404


def test_refresh_is_rejected_while_a_turn_is_generating(client):
    from app.routers import adventures

    sid = make_scenario(client, title="Keep", memory="Old")
    adv_id = start(client, sid)
    adventures._active_turns.add(adv_id)
    try:
        r = client.post(f"/api/adventures/{adv_id}/refresh", json={})
        assert r.status_code == 409
    finally:
        adventures._active_turns.discard(adv_id)
