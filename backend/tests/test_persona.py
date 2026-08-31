"""Phase 18: the adventure's persona — who the player is playing as.

Before this existed the protagonist had no name anywhere in the prompt. The
player's stat block rendered as `You: hp 100/100` and the summarizer was handed
second-person prose with nothing to say who "you" was.

The rules this file holds in place:

* An empty `persona_name` behaves exactly as the app did before personas
  existed, so an adventure that predates the migration is not a special case.
* The persona is in the **system** block. It is user-only, so it never changes
  during a story, and putting it in the cached prefix is what makes it free.
* A persona works without an RPG layer. That is the case it was added for.
* Naming the block does not rename the path. The model still writes
  `player.hp`, and a delta aimed at `persona.*` is refused.

    python -m pytest tests/test_persona.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models, worldstate
from app.context import builder
from app.database import Base, SessionLocal, engine, get_db
from app.main import app

SCHEMA = {
    "player": {"hp": {"min": 0, "max": 100, "initial": 100, "desc": "Health"}},
    "world": {"day": {"min": 1, "initial": 1, "desc": "Day"}},
}


def _persona(**kwargs) -> models.Adventure:
    """An unsaved adventure carrying only the fields `render_persona` reads."""
    return models.Adventure(
        persona_name=kwargs.get("name", ""),
        persona_pronouns=kwargs.get("pronouns", ""),
        persona_desc=kwargs.get("desc", ""),
    )


# ------------------------------------------------------------- the rendering

def test_no_persona_renders_nothing():
    assert builder.render_persona(_persona()) == ""


def test_whitespace_only_is_the_same_as_empty():
    """The columns default to '', but a player can clear a field to spaces."""
    assert builder.render_persona(_persona(name="  ", pronouns=" ", desc="\n")) == ""


def test_name_and_pronouns_and_description():
    text = builder.render_persona(
        _persona(name="Kaelen", pronouns="he/him", desc="A half-elf ranger.")
    )
    assert text.startswith("Player character:\n")
    assert "You are Kaelen (he/him)." in text
    assert "A half-elf ranger." in text


def test_name_alone_is_enough():
    text = builder.render_persona(_persona(name="Kaelen"))
    assert "You are Kaelen." in text
    assert "(" not in text, "no empty pronoun bracket"


def test_description_alone_is_enough():
    """A player who names no character still gets their description through."""
    text = builder.render_persona(_persona(desc="A nameless wanderer."))
    assert "A nameless wanderer." in text
    assert "You are" not in text


# ---------------------------------------------------- the world-state labels

def test_state_section_labels_the_block_with_the_name():
    state = worldstate.instantiate(SCHEMA)
    block = worldstate.render_state_section(state, SCHEMA, {}, "Kaelen")
    # The path travels with the name. Without it the model reads "Kaelen" in
    # the prose and writes `kaelen.hp`, which `apply_delta` then refuses.
    assert "Kaelen (player): hp 100/100" in block


def test_state_section_falls_back_to_you():
    state = worldstate.instantiate(SCHEMA)
    block = worldstate.render_state_section(state, SCHEMA, {}, "")
    assert "You: hp 100/100" in block
    assert "(player)" not in block


def test_reference_ties_the_name_to_the_path():
    guide = worldstate.render_reference(SCHEMA, "Kaelen")
    assert "Protagonist Kaelen" in guide
    assert "player.<stat>" in guide


def test_reference_without_a_persona_is_unchanged():
    assert "Protagonist" not in worldstate.render_reference(SCHEMA, "")


def test_reference_does_not_repeat_the_description():
    """The description has its own section. The guide is about paths."""
    guide = worldstate.render_reference(SCHEMA, "Kaelen")
    assert "half-elf" not in guide


# ------------------------------------------------------ the assembled prompt

@pytest.fixture()
def story():
    """An adventure with an RPG layer, a persona, and a little history."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="persona@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(user_id=user.id, api_key="enc:dummy", model="m")
    db.add(settings)
    scenario = models.Scenario(
        user_id=user.id, title="S", prompt="A road.", stat_schema=SCHEMA
    )
    db.add(scenario)
    db.flush()
    adventure = models.Adventure(
        user_id=user.id, title="A", scenario_id=scenario.id, script_state={},
        memory="The hero hunts bandits.",
        ai_instructions="Write in second person.",
        story_summary="The hero left the village.",
        world_state=worldstate.instantiate(SCHEMA),
        persona_name="Kaelen",
        persona_pronouns="he/him",
        persona_desc="A half-elf ranger, exiled from the northern holds.",
    )
    db.add(adventure)
    db.flush()
    for i in range(6):
        db.add(models.Action(adventure_id=adventure.id,
                             type="ai" if i % 2 else "do",
                             text=f"[{i}] The road bends onward past the treeline."))
    db.commit()
    db.expire_all()
    adventure = db.get(models.Adventure, adventure.id)
    settings = db.get(models.Settings, settings.id)
    try:
        yield db, adventure, settings
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_persona_is_in_the_system_block(story):
    """It is user-only, so it belongs in the cached prefix rather than below
    the history with the values that move."""
    db, adventure, settings = story
    system_text, story_text, _ = builder.build_context(adventure, settings)
    assert "You are Kaelen (he/him)." in system_text
    assert "A half-elf ranger" in system_text
    assert "Kaelen (he/him)" not in story_text


def test_persona_does_not_move_between_turns(story):
    """The static block has to be byte-identical across turns. A persona that
    changed with the world state would re-price the whole history."""
    db, adventure, settings = story
    before, _, _ = builder.build_context(adventure, settings)
    adventure.world_state = {**adventure.world_state, "player": {"hp": 40}}
    db.commit()
    after, story_text, _ = builder.build_context(adventure, settings)
    assert before == after
    assert "Kaelen (player): hp 40/100" in story_text


def test_persona_sits_above_the_plot_essentials(story):
    db, adventure, settings = story
    _, _, report = builder.build_context(adventure, settings)
    labels = [s["label"] for s in report["sections"]]
    assert labels.index("persona") < labels.index("plot_essentials")


def test_no_persona_section_when_the_fields_are_blank(story):
    db, adventure, settings = story
    adventure.persona_name = ""
    adventure.persona_pronouns = ""
    adventure.persona_desc = ""
    db.commit()
    system_text, story_text, report = builder.build_context(adventure, settings)
    assert "persona" not in [s["label"] for s in report["sections"]]
    assert "Player character" not in system_text
    assert "You: hp 100/100" in story_text


def test_persona_is_charged_to_the_token_budget(story):
    """It moved into `system_sections`, and everything there is counted in
    `reserved`. If it were not, the history would overrun the budget."""
    db, adventure, settings = story
    _, _, report = builder.build_context(adventure, settings)
    persona = next(s for s in report["sections"] if s["label"] == "persona")
    assert persona["tokens"] > 0
    assert report["tokens"]["total"] >= persona["tokens"]


# --------------------------------------------- a persona with no RPG layer

@pytest.fixture()
def plain():
    """A blank adventure: no scenario, so no `stat_schema` at all."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="plain@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(user_id=user.id, api_key="enc:dummy", model="m")
    db.add(settings)
    adventure = models.Adventure(
        user_id=user.id, title="A", script_state={}, world_state={},
        persona_name="Wren", persona_pronouns="they/them",
        persona_desc="A courier who reads other people's letters.",
    )
    db.add(adventure)
    db.flush()
    db.add(models.Action(adventure_id=adventure.id, type="do", text="Walk east."))
    db.commit()
    db.expire_all()
    adventure = db.get(models.Adventure, adventure.id)
    settings = db.get(models.Settings, settings.id)
    try:
        yield db, adventure, settings
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_persona_works_without_an_rpg_layer(plain):
    """The case the persona exists for. It must not be gated behind `has_ws`."""
    db, adventure, settings = plain
    system_text, _, _ = builder.build_context(adventure, settings)
    assert "You are Wren (they/them)." in system_text
    assert "A courier who reads other people's letters." in system_text


# ------------------------------------------------- the AI cannot rewrite it

@pytest.mark.parametrize("path", ["persona.name", "persona.desc", "persona"])
def test_a_delta_aimed_at_the_persona_is_refused(path):
    """Nothing was built for this: `_resolve` rejects unknown paths already.
    The test holds the behavior in place, because the persona sitting in the
    cached prefix depends on the model being unable to move it."""
    state = worldstate.instantiate(SCHEMA)
    _, report = worldstate.apply_delta(state, SCHEMA, {path: "Bob"}, 1)
    assert [r["path"] for r in report["rejected"]] == [path]
    assert not report["applied"]


def test_the_player_path_still_works_with_a_persona_set():
    """Naming the block does not rename the path."""
    state = worldstate.instantiate(SCHEMA)
    new_state, report = worldstate.apply_delta(state, SCHEMA, {"player.hp": -15}, 1)
    assert new_state["player"]["hp"] == 85
    assert [r["path"] for r in report["applied"]] == ["player.hp"]


# ------------------------------------- setting it at the start, editing it later

@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="persona-api@example.com")
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
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def test_a_persona_can_be_named_when_the_adventure_is_created(client):
    """The modal that used to appear only for `${Placeholder}` scenarios now
    always opens, and this is what it posts."""
    r = client.post("/api/adventures", json={
        "title": "A", "persona_name": "Kaelen", "persona_pronouns": "he/him",
        "persona_desc": "A half-elf ranger.",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["persona_name"] == "Kaelen"
    assert body["persona_pronouns"] == "he/him"
    assert body["persona_desc"] == "A half-elf ranger."


def test_creating_without_a_persona_still_works(client):
    """Every field is optional. A blank adventure is the old behavior."""
    r = client.post("/api/adventures", json={"title": "A"})
    assert r.status_code == 201, r.text
    assert r.json()["persona_name"] == ""


def test_the_persona_is_stripped_on_the_way_in(client):
    r = client.post("/api/adventures", json={
        "title": "A", "persona_name": "  Kaelen  ", "persona_desc": " ranger \n",
    })
    assert r.json()["persona_name"] == "Kaelen"
    assert r.json()["persona_desc"] == "ranger"


def test_the_persona_can_be_edited_later(client):
    """A player renames their character mid-story; nothing else moves."""
    adv_id = client.post("/api/adventures", json={
        "title": "A", "persona_name": "Kaelen",
    }).json()["id"]
    r = client.patch(f"/api/adventures/{adv_id}", json={"persona_name": "Aria"})
    assert r.status_code == 200, r.text
    assert r.json()["persona_name"] == "Aria"
    assert client.get(f"/api/adventures/{adv_id}").json()["persona_name"] == "Aria"


def test_an_over_long_name_is_refused_rather_than_truncated(client):
    """The column is VARCHAR(80). A 422 here is a 500 at INSERT otherwise."""
    r = client.post("/api/adventures", json={"title": "A", "persona_name": "K" * 81})
    assert r.status_code == 422


def test_placeholders_and_the_persona_are_independent(client):
    """A scenario asking for `${Name}` is asking its own question. Nothing
    fills it in from the persona, and nothing fills the persona from it."""
    db = SessionLocal()
    scenario = models.Scenario(
        user_id=db.query(models.User).first().id,
        title="S", prompt="A guard sneers at ${Name}.",
    )
    db.add(scenario)
    db.commit()
    scenario_id = scenario.id
    db.close()

    adv = client.post("/api/adventures", json={
        "scenario_id": scenario_id,
        "persona_name": "Kaelen",
        "placeholders": {"Name": "Wren"},
    }).json()
    assert adv["persona_name"] == "Kaelen"
    opening = next(a for a in adv["actions"] if a["type"] == "start")
    assert "A guard sneers at Wren." == opening["text"]
