"""Prompt caching: the prompt has to start with the same bytes every turn.

Every endpoint that caches prompts caches a prefix. It reuses the request
up to the first byte that differs from last time, and no further. The cost
of a turn is therefore decided by layout: one section that changes each
turn, placed near the top, re-prices everything underneath it, and
underneath it is the story history, which makes up most of the prompt.

Three things must hold, and each is easy to undo by accident:

* The static block is byte-identical across turns. Adding a section that
  moves (live stats, retrieved memories, a rewritten summary) to
  `system_sections` is the mistake this file exists to catch.
* The sections that move sit after the history, but still before the tail
  that is last for its own reasons: front memory, the length hint, and
  `EMIT_REMINDER`, which is what keeps the state block emitted at all.
* Moving a section out of the system block does not drop it from the
  token budget. It is still in the prompt.

This file also covers two request-level concerns: preferring one
OpenRouter upstream, since each upstream holds its own cache, and reading
back the usage the endpoint reports, so the hit rate is measurable rather
than assumed.

    python -m pytest tests/test_prompt_caching.py -v
"""
import os


import pytest

from app import models, worldstate
from app.context import builder
from app.database import Base, SessionLocal, engine
from app.providers.openai_compatible import OpenAICompatibleProvider

SCHEMA = {
    "player": {"hp": {"min": 0, "max": 100, "initial": 100, "desc": "Health"}},
    "world": {"alarm": {"min": 0, "max": 10, "initial": 0, "desc": "Alarm level"}},
}


# ------------------------------------------------- preferring one upstream

def _routed(endpoint, model):
    provider = OpenAICompatibleProvider(endpoint, "k", model, "chat", 0)
    body = {"max_tokens": 100}
    provider._apply_provider_routing(body)
    return body


def test_openrouter_deepseek_pins_the_upstream():
    """Each upstream has its own cache, so routing has to be deterministic."""
    body = _routed("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash-0731")
    assert body["provider"] == {"order": ["deepseek"]}


def test_fallbacks_stay_on():
    """A preference, not a restriction: if deepseek is down the turn still runs
    somewhere else and merely misses the cache."""
    body = _routed("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash-0731")
    assert "allow_fallbacks" not in body["provider"]


def test_non_openrouter_endpoints_get_no_provider_field():
    """Ollama and other providers reject fields they do not know. This is
    the same problem the `reasoning` param works around."""
    body = _routed("http://localhost:11434/v1", "deepseek/deepseek-v4-flash-0731")
    assert "provider" not in body


def test_unknown_vendors_are_left_alone():
    """The vendor half of a slug is not reliably a provider slug: Google's
    models are served by "google-ai-studio", and there is no "google". A guess
    would be a routing preference naming an upstream that does not exist."""
    body = _routed("https://openrouter.ai/api/v1", "google/gemma-4-26b-a4b-it:free")
    assert "provider" not in body


# ------------------------------------------------------ reading usage back

def test_usage_is_recorded_from_a_final_chunk():
    """In a stream, the usage block arrives in a final chunk that carries
    no choices, which is why it is read separately from the text
    extraction."""
    provider = OpenAICompatibleProvider("https://openrouter.ai/api/v1", "k", "m")
    assert provider.last_usage is None
    provider._record_usage({"choices": [{"delta": {"content": "hi"}}]})
    assert provider.last_usage is None
    provider._record_usage(
        {"choices": [], "usage": {"prompt_tokens": 900,
                                  "prompt_tokens_details": {"cached_tokens": 768}}}
    )
    assert provider.last_usage["prompt_tokens_details"]["cached_tokens"] == 768


def test_a_later_chunk_without_usage_does_not_erase_it():
    provider = OpenAICompatibleProvider("https://openrouter.ai/api/v1", "k", "m")
    provider._record_usage({"usage": {"prompt_tokens": 5}})
    provider._record_usage({"choices": [{"delta": {"content": "x"}}]})
    provider._record_usage({"usage": {}})
    assert provider.last_usage == {"prompt_tokens": 5}


# ------------------------------------------------------------ prompt layout

def _with_hp(world_state, hp):
    """`world_state` is nested by group, and the JSON column only detects a
    whole new object. Build a new one instead of mutating in place."""
    return {**world_state, "player": {**world_state["player"], "hp": hp}}


@pytest.fixture()
def story():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="cache@example.com")
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
    )
    db.add(adventure)
    db.flush()
    for i in range(6):
        db.add(models.Action(adventure_id=adventure.id, index=i,
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


def test_changing_a_stat_leaves_the_static_block_untouched(story):
    """The whole point. Live values used to sit third from the top, so a
    single point of damage re-priced the instructions, the plot, and the
    history."""
    db, adventure, settings = story
    before, _, _ = builder.build_context(adventure, settings)
    adventure.world_state = _with_hp(adventure.world_state, 40)
    db.commit()
    after, story_text, _ = builder.build_context(adventure, settings)
    assert before == after
    assert "hp 40/100" in story_text, "the new value still has to reach the model"


def test_the_static_block_holds_the_things_that_do_not_move(story):
    db, adventure, settings = story
    system_text, story_text, _ = builder.build_context(adventure, settings)
    for fixed in ("Write in second person.", "The hero hunts bandits."):
        assert fixed in system_text
    # The stat guide is derived from the schema, so it is fixed. The live
    # values it describes are not fixed, and belong to the story text.
    assert "Stat guide" in system_text
    for moves in ("The hero left the village.", "hp 100/100"):
        assert moves not in system_text
        assert moves in story_text


def test_volatile_sections_sit_after_the_history(story):
    db, adventure, settings = story
    _, story_text, _ = builder.build_context(adventure, settings)
    history_at = story_text.index("[5] The road bends")
    for label in ("Story summary:", "World state"):
        assert story_text.index(label) > history_at, label


def test_the_tail_stays_the_tail(story):
    """Front memory, the length hint, and `EMIT_REMINDER` are last for
    reasons of their own, and the live sections must not have displaced
    them."""
    db, adventure, settings = story
    _, story_text, report = builder.build_context(adventure, settings)
    labels = [s["label"] for s in report["sections"]]
    assert labels[-1] == "world_state_reminder"
    assert labels[-2] == "length_hint"
    assert labels.index("world_state") < labels.index("length_hint")
    assert story_text.rstrip().endswith(worldstate.EMIT_REMINDER.rstrip())


def test_live_sections_are_still_charged_to_the_budget(story):
    """They moved out of `system_sections`, so it would be easy to stop
    counting them in `reserved`. If that happened, the history, which is
    budgeted with what is left over, would quietly overrun."""
    db, adventure, settings = story
    for i in range(6, 90):
        db.add(models.Action(
            adventure_id=adventure.id, index=i, type="do",
            text=f"[{i}] " + "The road bends onward past the treeline. " * 6,
        ))
    settings.context_token_budget = 4000
    db.commit()
    db.expire_all()
    adventure = db.get(models.Adventure, adventure.id)
    settings = db.get(models.Settings, settings.id)

    _, _, lean = builder.build_context(adventure, settings)
    adventure.story_summary = "The hero left the village. " * 150
    db.commit()
    _, _, fat = builder.build_context(adventure, settings)

    assert fat["history"]["included"] < lean["history"]["included"], (
        "a bigger summary has to leave less room for history"
    )


def test_a_new_turn_only_appends_to_the_cached_prefix(story):
    """Playing on must extend the previous prompt, not rewrite it: the shared
    prefix has to still contain the whole static block and the older history."""
    db, adventure, settings = story
    system_a, story_a, _ = builder.build_context(adventure, settings)
    db.add(models.Action(adventure_id=adventure.id, index=6, type="do",
                         text="[6] You step into the clearing."))
    db.commit()
    db.expire_all()
    adventure = db.get(models.Adventure, adventure.id)
    system_b, story_b, _ = builder.build_context(adventure, settings)

    assert system_a == system_b
    shared = os.path.commonprefix([story_a, story_b])
    assert "[0] The road bends" in shared
    assert "[5] The road bends" in shared
