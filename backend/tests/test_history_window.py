"""The context builder reads a window of the story, not all of it.

Walking `adventure.actions` every turn made a turn cost O(story length), so a
long adventure read hundreds of KB to use the tail of it — and the cost grew
with every turn played. `app.context.history` serves tails, slices and counts
from SQL instead.

Two things have to hold, and both are easy to break by accident:

* the window must produce **exactly** the prompt the full story produced, or
  this is a behaviour change wearing an optimization's clothes;
* the helpers must agree with the old list arithmetic, because memorybank's
  cursors are *positions* in that list and a cursor off by one silently
  summarizes the wrong actions.

    python -m pytest tests/test_history_window.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest
from sqlalchemy import event

from app import memorybank, models
from app.context import builder, history
from app.database import Base, SessionLocal, engine

# Long enough that a window is much smaller than the whole story.
ACTION_COUNT = 200
NARRATION = (
    "The scrub gives way to a shallow bowl of land where woodsmoke hangs in "
    "flat grey layers, and somewhere behind the largest tent a woman is "
    "arguing, low and fast. "
) * 3

SCHEMA = {
    "player": {"hp": {"min": 0, "max": 100, "initial": 100, "desc": "Health"}},
    "npcs": {
        "gwen": {"name": "Gwen", "keys": ["gwen"], "desc": "A scout.",
                 "stats": {"trust": {"min": 0, "max": 100, "initial": 30}}},
    },
}


@pytest.fixture()
def story():
    """An adventure with ACTION_COUNT actions, plus its settings."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="window@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(user_id=user.id, api_key="enc:dummy", model="m")
    db.add(settings)
    scenario = models.Scenario(user_id=user.id, title="S", stat_schema=SCHEMA,
                               prompt="A long road." * 50)
    db.add(scenario)
    db.flush()
    adventure = models.Adventure(
        user_id=user.id, title="Long", scenario_id=scenario.id, script_state={},
        memory="The hero is hunting bandits. " * 20,
        world_state={"player": {"hp": 100}, "npc": {"gwen": {"trust": 30}},
                     "milestones": {}, "flags": {}, "_meta": {"last_changed": {}}},
    )
    db.add(adventure)
    db.flush()
    db.add(models.StoryCard(adventure_id=adventure.id, name="Gwen", keys="gwen",
                            entry="A scout with sharp eyes.", type="lore"))
    for i in range(ACTION_COUNT):
        db.add(models.Action(
            adventure_id=adventure.id, index=i,
            type="ai" if i % 2 else "do",
            text=f"[{i}] {NARRATION}",
            world_delta={"delta": {"player.hp": -1},
                         "applied": [{"path": "player.hp", "old": 100, "new": 99}]},
        ))
    db.commit()
    db.expire_all()
    adventure = db.get(models.Adventure, adventure.id)
    settings = db.get(models.Settings, settings.id)
    try:
        yield db, adventure, settings
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def full_window(adventure, budget_tokens, token_counter, exclude_action_id=None):
    """Stand-in for window_covering that hands back the entire story, i.e. the
    behaviour this module replaced."""
    return history.story_actions(adventure, exclude_action_id)


@pytest.fixture()
def actions_loaded():
    """Counts Action rows the ORM materializes, i.e. how much of the story was
    actually fetched. rowcount is meaningless for SELECT on SQLite, so count
    the objects the mapper builds instead."""
    loaded = {"n": 0}

    def on_load(target, context):
        loaded["n"] += 1

    event.listen(models.Action, "load", on_load)
    try:
        yield loaded
    finally:
        event.remove(models.Action, "load", on_load)


# ------------------------------------------------------- the prompt is equal

@pytest.mark.parametrize("budget", [1024, 4096, 8192, 16384, 65536])
def test_window_builds_the_same_prompt_as_the_whole_story(story, budget, monkeypatch):
    db, adventure, settings = story
    settings.context_token_budget = budget

    windowed = builder.build_context(adventure, settings)
    monkeypatch.setattr(builder.history, "window_covering", full_window)
    db.expire(adventure)
    everything = builder.build_context(adventure, settings)

    assert windowed[0] == everything[0], "system prompt differs"
    assert windowed[1] == everything[1], "story prompt differs"
    assert windowed[2]["history"] == everything[2]["history"]
    assert windowed[2]["cards"] == everything[2]["cards"]


def test_window_matches_on_the_retry_shape(story, monkeypatch):
    """Retry excludes the action being regenerated; the exclusion has to reach
    the window query, not just the in-memory filter."""
    db, adventure, settings = story
    last = history.tail(adventure, 1)[0]

    windowed = builder.build_context(adventure, settings, exclude_action_id=last.id)
    assert f"[{last.index}]" not in windowed[1]

    monkeypatch.setattr(builder.history, "window_covering", full_window)
    db.expire(adventure)
    everything = builder.build_context(adventure, settings, exclude_action_id=last.id)
    assert windowed[1] == everything[1]


def test_reported_total_is_the_whole_story_not_the_window(story):
    """Insights says "N of M actions included"; M must not become the window."""
    db, adventure, settings = story
    settings.context_token_budget = 4096
    report = builder.build_context(adventure, settings)[2]
    assert report["history"]["total"] == ACTION_COUNT
    assert report["history"]["included"] < ACTION_COUNT


# ------------------------------------------------------------ it is bounded

def test_building_context_reads_far_less_than_the_whole_story(story, actions_loaded):
    db, adventure, settings = story
    # Expire first: expiring afterwards would discard the unflushed change and
    # silently put the budget back to its default.
    db.expire_all()
    # Small enough that the budget, not the length of the story, decides.
    settings.context_token_budget = 4096
    actions_loaded["n"] = 0

    report = builder.build_context(adventure, settings)[2]
    included = report["history"]["included"]

    assert included < ACTION_COUNT, "fixture is too short to prove anything"
    # The window aims a margin past the budget and re-asks if it fell short, so
    # it reads somewhat more than it includes. What matters is that the read is
    # a function of the token budget, not of how long the story has got.
    assert actions_loaded["n"] < ACTION_COUNT // 2, (
        f"read {actions_loaded['n']} action rows out of {ACTION_COUNT} to "
        f"include {included} — the window is not bounding the read"
    )


def test_window_is_ordered_and_free_of_duplicates(story):
    """The window grows by fetching only what it does not already hold, so an
    off-by-one in the offset would show up as a repeated or missing action."""
    db, adventure, settings = story
    window = history.window_covering(adventure, 16384, builder.count_tokens)
    ids = [a.id for a in window]
    assert len(ids) == len(set(ids)), "the same action appeared twice in the window"
    assert ids == sorted(ids), "window must be oldest-first"


# ------------------------------------------- the node-anchored reads agree

def test_helpers_agree_with_the_full_list(story):
    db, adventure, settings = story
    actions = history.story_actions(adventure)
    assert len(actions) == ACTION_COUNT

    assert history.count(adventure) == len(actions)
    assert history.max_action_index(adventure) == max(a.index for a in actions)
    assert [a.id for a in history.tail(adventure, 4)] == [a.id for a in actions[-4:]]
    assert [a.id for a in history.slice_(adventure, 10, 6)] == [a.id for a in actions[10:16]]
    assert [a.id for a in history.tail_range(adventure, 5, 3)] == \
        [a.id for a in actions[-8:-5]]
    assert memorybank.settled_count(adventure) == len(actions) - 1
    assert history.newest_settled(adventure).id == actions[-2].id

    for probe in (0, 1, ACTION_COUNT // 2, ACTION_COUNT - 1):
        boundary = actions[probe].depth
        assert history.count_after(adventure, boundary) == ACTION_COUNT - probe - 1
        assert [a.id for a in history.after(adventure, boundary, 3)] == \
            [a.id for a in actions[probe + 1:probe + 4]]


def test_a_depth_boundary_survives_a_middle_action_being_deleted(story):
    """The case that has broken the cursors twice before, and the reason they
    are depths now.

    A *position* answers "how much story is past this point?" by counting from
    the start, so deleting anything in front of the mark changes which action
    the mark names. A depth names the same node either way — the only thing
    that changes is the count of what comes after, which is what did change.
    """
    db, adventure, settings = story
    actions = history.story_actions(adventure)
    mark = actions[30].depth
    before = history.count_after(adventure, mark)
    next_three = [a.id for a in history.after(adventure, mark, 3)]

    victim = actions[10]  # in front of the mark
    db.delete(victim)
    db.commit()
    db.expire(adventure)

    assert history.count(adventure) == ACTION_COUNT - 1
    assert history.count_after(adventure, mark) == before, "the mark moved"
    assert [a.id for a in history.after(adventure, mark, 3)] == next_three

    # ...and deleting something *after* it is the one thing that does change
    # the count, because that is a fact about the story rather than about the
    # coordinate system.
    db.delete(history.after(adventure, mark, 1)[0])
    db.commit()
    db.expire(adventure)
    assert history.count_after(adventure, mark) == before - 1


def test_blank_actions_are_excluded_the_same_way_in_sql_and_python(story):
    """SQL and Python must agree on membership or a cursor points elsewhere."""
    db, adventure, settings = story
    for blank in ("", "   ", "\n", "\t\n "):
        db.add(models.Action(adventure_id=adventure.id,
                             index=history.max_action_index(adventure) + 1,
                             type="story", text=blank))
    db.commit()
    db.expire(adventure)

    # SQL path (relationship not loaded)
    from_sql = history.count(adventure)
    # Python path (relationship loaded)
    adventure.actions  # noqa: B018 — force the collection into memory
    from_python = history.count(adventure)

    assert from_sql == from_python == ACTION_COUNT
