"""Phase 18b: what the summarizer is told about who the characters are.

Before this, `_create_due_memories` sent six actions of second-person prose and
nothing else. The model had no way to know who "you" was or who "she" was, so
neither did any memory it wrote, and the summary built from those memories
inherited the problem.

The rules this file holds in place:

* The brief carries fixed descriptions only. A live stat in it would make the
  same event, summarized twice, come out framed differently — the exact fault
  the change exists to remove.
* Places and items are not topped up into the roster. Only characters are.
* An adventure with no persona and no cards still summarizes, with the prompt
  it had before.

    python -m pytest tests/test_cast_brief.py -v
"""
import asyncio

import pytest

from app import memorybank, models
from app.database import Base, SessionLocal, engine

GWEN = "A loyal ranger and the player's ally. Quick with a bow, fiercely protective."
LEADER = "Scarred, patient, and the one holding the strongbox key."
TAVERN = "A tavern three days south of the camp."


class StubSummarizer:
    """Records every (system, user) pair handed to the summarizer."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user))
        return f"Memory {len(self.calls)}."


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def make_adventure(db, *, actions=0, persona=True, cards=True, memory=True):
    user = models.User(is_guest=False, email="cast@example.com")
    db.add(user)
    db.flush()
    db.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    adventure = models.Adventure(
        user_id=user.id, title="Camp", script_state={}, auto_summarize=True,
        memory=("The player and Gwen are raiding a bandit camp to recover a "
                "stolen strongbox.") if memory else "",
        persona_name="Kaelen" if persona else "",
        persona_pronouns="he/him" if persona else "",
        persona_desc="A half-elf ranger, exiled from the northern holds." if persona else "",
    )
    db.add(adventure)
    db.flush()
    if cards:
        db.add(models.StoryCard(adventure_id=adventure.id, name="Gwen",
                                keys="Gwen, ranger, her", type="character", entry=GWEN))
        db.add(models.StoryCard(adventure_id=adventure.id, name="Bandit Leader",
                                keys="Bandit Leader, leader", type="character", entry=LEADER))
        db.add(models.StoryCard(adventure_id=adventure.id, name="The Rusted Tankard",
                                keys="tankard, tavern", type="location", entry=TAVERN))
    for i in range(actions):
        db.add(models.Action(adventure_id=adventure.id,
                             type="ai" if i % 2 else "do",
                             text=f"You walk on. Action {i}."))
    db.commit()
    db.refresh(adventure)
    return adventure


# ------------------------------------------------------------ the brief itself

def test_the_protagonist_is_named_and_marked_as_such(db):
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "You walk into the camp.")
    assert "- Kaelen (he/him) — the protagonist." in brief
    assert "A half-elf ranger" in brief


def test_a_named_character_in_the_text_is_in_the_roster(db):
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "Gwen checks her bowstring.")
    assert "- Gwen — " in brief and "Quick with a bow" in brief


def test_a_character_referred_to_only_by_pronoun_is_still_in_the_roster(db):
    """The block that most needs a cast is the one written in bare pronouns.
    Keyword matching alone finds nothing here, so the roster is topped up."""
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "She grabs your arm before you step through.")
    assert "- Gwen — " in brief
    assert "- Bandit Leader — " in brief


def test_places_are_not_topped_up(db):
    """An unmentioned tavern is not who "she" was."""
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "She grabs your arm.")
    assert "Rusted Tankard" not in brief


def test_a_place_that_is_mentioned_does_appear(db):
    """Topping up is limited to characters. Matching is not."""
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "You push into the tavern, breathless.")
    assert "The Rusted Tankard" in brief


def test_the_setting_is_the_plot_essentials(db):
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "You walk on.")
    assert "Setting:\nThe player and Gwen are raiding a bandit camp" in brief


def test_no_persona_and_no_cards_gives_no_brief(db):
    adventure = make_adventure(db, persona=False, cards=False, memory=False)
    assert memorybank.cast_brief(adventure, "You walk on.") == ""


def test_a_persona_alone_is_enough_for_a_brief(db):
    adventure = make_adventure(db, cards=False, memory=False)
    brief = memorybank.cast_brief(adventure, "You walk on.")
    assert brief.startswith("Cast:\n- Kaelen (he/him) — the protagonist.")


def test_an_unnamed_protagonist_is_called_the_player(db):
    adventure = make_adventure(db, cards=False, memory=False)
    adventure.persona_name = ""
    adventure.persona_pronouns = ""
    db.commit()
    assert "- The player — the protagonist." in memorybank.cast_brief(adventure, "x")


def test_the_roster_is_capped(db, monkeypatch):
    monkeypatch.setattr(memorybank, "MAX_CAST_MEMBERS", 2)
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "She grabs your arm.")
    assert len([ln for ln in brief.splitlines() if ln.startswith("- ")]) == 2


def test_a_long_description_is_trimmed(db):
    adventure = make_adventure(db, cards=False, memory=False)
    adventure.persona_desc = "word " * 400
    db.commit()
    brief = memorybank.cast_brief(adventure, "x")
    assert "…" in brief
    assert len(brief) < 600


def test_a_character_is_never_listed_twice(db):
    """The persona and a card can carry the same name."""
    adventure = make_adventure(db, cards=False, memory=False)
    db.add(models.StoryCard(adventure_id=adventure.id, name="Kaelen",
                            keys="Kaelen", type="character", entry="Also Kaelen."))
    db.commit()
    db.refresh(adventure)
    brief = memorybank.cast_brief(adventure, "Kaelen walks on.")
    assert brief.lower().count("kaelen") == 1


def test_the_brief_holds_no_live_values(db):
    """Fixed descriptions only. A stat here would frame the same event two ways
    depending on when it happened to be summarized."""
    adventure = make_adventure(db)
    brief = memorybank.cast_brief(adventure, "Gwen checks her bowstring.")
    for live in ("hp", "trust", "/100", "wary", "healthy"):
        assert live not in brief.lower()


# ------------------------------------------------- what actually gets sent

def _run_memory_pass(db, adventure, monkeypatch, stub):
    monkeypatch.setattr(memorybank, "MEMORY_START", 0)
    monkeypatch.setattr(memorybank, "MEMORY_INTERVAL", 4)
    monkeypatch.setattr(memorybank, "MAX_MEMORIES_PER_RUN", 1)
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    settings = db.query(models.Settings).first()
    asyncio.run(memorybank._create_due_memories(adventure, settings, db))
    assert stub.calls, "the summarizer was never called"
    return stub.calls[0]


def test_the_memory_prompt_carries_the_brief_above_the_excerpt(db, monkeypatch):
    adventure = make_adventure(db, actions=6)
    system, user = _run_memory_pass(db, adventure, monkeypatch, StubSummarizer())
    assert user.index("Cast:") < user.index("Story excerpt:")
    assert "Kaelen (he/him) — the protagonist" in user
    assert "Setting:" in user


def test_the_memory_prompt_demands_third_person_and_names(db, monkeypatch):
    adventure = make_adventure(db, actions=6)
    system, _ = _run_memory_pass(db, adventure, monkeypatch, StubSummarizer())
    assert "third person" in system
    assert 'never as "you"' in system


def test_an_adventure_with_nothing_to_say_sends_the_old_prompt(db, monkeypatch):
    """No persona, no cards, no plot essentials: the user message is exactly
    what it was before this change, with no stray blank lines."""
    adventure = make_adventure(db, actions=6, persona=False, cards=False, memory=False)
    _, user = _run_memory_pass(db, adventure, monkeypatch, StubSummarizer())
    assert user.startswith("Story excerpt:")
    assert "Cast:" not in user


def test_the_summary_prompt_carries_the_brief_too(db, monkeypatch):
    """It builds from the memories, so it inherits their framing — but the
    fallback hands it raw second-person story text, which needs the brief."""
    adventure = make_adventure(db, actions=20)
    stub = StubSummarizer()
    monkeypatch.setattr(memorybank, "SUMMARY_INTERVAL", 1)
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    settings = db.query(models.Settings).first()
    asyncio.run(memorybank._update_story_summary(adventure, settings, db))
    assert stub.calls, "the summarizer was never called"
    system, user = stub.calls[0]
    assert user.index("Cast:") < user.index("Current story summary:")
    assert "Kaelen (he/him) — the protagonist" in user
    assert "third person" in system
    assert adventure.story_summary == "Memory 1."
