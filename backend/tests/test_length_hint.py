"""The turn prompt asks for a turn that fits inside `max_output_tokens`.

`max_output_tokens` is a hard limit the endpoint enforces mid-sentence. The
state block is emitted after the narration, so a long turn hits the limit
partway through the block, and the deltas are lost. Nothing reads
`finish_reason`, so this loss happens silently. The prompt now carries a
word budget derived from the cap, so the model lands just inside it.

Two things are easy to break here:

* The hint must be stated in words, not tokens. A model cannot count its
  own tokens, and a hint it cannot follow is wasted budget.
* The hint must not displace `EMIT_REMINDER` from the last position, which
  is the whole mechanism that keeps the state block emitted at all (see
  test_worldstate.py and the emit-reliability fix).

    python -m pytest tests/test_length_hint.py -v
"""
import re


import pytest

from app import models, worldstate
from app.context import builder
from app.database import Base, SessionLocal, engine

SCHEMA = {
    "player": {"hp": {"min": 0, "max": 100, "initial": 100, "desc": "Health"}},
}


@pytest.fixture()
def story():
    """A short adventure, with and without a stat schema on demand."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="length@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(user_id=user.id, api_key="enc:dummy", model="m")
    db.add(settings)
    scenario = models.Scenario(user_id=user.id, title="S", prompt="A road.")
    db.add(scenario)
    db.flush()
    adventure = models.Adventure(
        user_id=user.id, title="A", scenario_id=scenario.id, script_state={},
        memory="The hero hunts bandits.",
    )
    db.add(adventure)
    db.flush()
    for i in range(4):
        db.add(models.Action(adventure_id=adventure.id,
                             type="ai" if i % 2 else "do", text=f"[{i}] Onward."))
    db.commit()
    db.expire_all()
    adventure = db.get(models.Adventure, adventure.id)
    settings = db.get(models.Settings, settings.id)
    try:
        yield db, adventure, settings, scenario.id
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def with_schema(db, scenario_id, adventure):
    scenario = db.get(models.Scenario, scenario_id)
    scenario.stat_schema = SCHEMA
    adventure.world_state = worldstate.instantiate(SCHEMA)
    db.commit()
    db.expire_all()


# ----------------------------------------------------- the hint itself

def test_budget_is_the_cap_minus_headroom_and_buffer_in_words():
    """800-token cap → 750 after headroom → ~562 words → 506 after the buffer."""
    hint = builder.length_hint(800, has_ws=True)
    assert "506" in hint
    assert "token" not in hint.lower(), "a model cannot count its own tokens"


def test_budget_tracks_the_setting():
    small = builder.length_hint(800, has_ws=True)
    large = builder.length_hint(2400, has_ws=True)
    assert small != large
    assert "1586" in large


def asked_words(cap):
    return int(re.search(r"(\d+) words", builder.length_hint(cap, has_ws=True)).group(1))


def test_buffer_leaves_room_for_overshoot():
    """The stated number must sit meaningfully under the real ceiling, or an
    on-target-but-slightly-long turn still hits the limit."""
    for cap in (400, 800, 1500, 2400):
        asked = asked_words(cap)
        ceiling = (cap - builder.LENGTH_HEADROOM) * builder.WORDS_PER_TOKEN
        assert asked < ceiling
        assert asked >= ceiling * 0.85, "buffer so large the hint wastes the cap"


def test_hint_is_phrased_as_a_ceiling_not_a_budget():
    """Measured: budget phrasing ("keep this turn under about N words")
    reads as a target to fill. It moved the mean turn from 174 to 246
    words, toward the limit it exists to avoid. The limit framing must
    survive future prompt edits."""
    hint = builder.length_hint(800, has_ws=True)
    assert "must not exceed" in hint
    assert "under about" not in hint
    assert "lower end" in hint, "without this the number still reads as a target"


def test_hint_states_a_floor_as_well_as_a_ceiling():
    """A ceiling alone is one-sided: a terse model has nothing to act on but
    the "only as much as the moment needs" clause and produces only two
    paragraphs. The floor is what makes the same prompt produce a similar
    length across models with different tendencies."""
    hint = builder.length_hint(800, has_ws=True)
    assert "506" in hint and "177" in hint
    assert "should not stop short of" in hint
    # Asymmetric on purpose: the ceiling is a hard limit and the floor is a
    # soft target, and neither is phrased as a specific number to reach.
    assert hint.index("must not exceed") < hint.index("should not stop short of")


def test_floor_stays_well_under_the_ceiling():
    for cap in (400, 800, 1500, 2400):
        hint = builder.length_hint(cap, has_ws=True)
        ceiling, floor = (int(n) for n in re.findall(r"(\d+)", hint)[:2])
        assert floor < ceiling * 0.5


def test_floor_is_dropped_when_the_cap_is_too_tight_for_one():
    """At a tight cap a short turn is the correct turn. The tight-cap
    wording is the one measured to keep the state block from being
    truncated (0/6 truncations at cap 250, against 2/6 unhinted), so it
    is left exactly as it was."""
    hint = builder.length_hint(250, has_ws=True)
    assert "should not stop short of" not in hint
    assert "much shorter" in hint


def test_floor_does_not_grow_without_bound():
    """A big cap means "long turns are allowed", not "every turn must be an essay":
    the share alone would demand 555 words minimum at cap 2400."""
    hint = builder.length_hint(2400, has_ws=True)
    assert str(builder.MAX_LENGTH_FLOOR_WORDS) in hint


def test_no_hint_when_the_cap_is_too_small_to_phrase():
    """Under the floor the hint is noise the model pays for in context."""
    assert builder.length_hint(100, has_ws=True) == ""
    assert builder.length_hint(builder.LENGTH_HEADROOM, has_ws=True) == ""
    assert builder.length_hint(0, has_ws=True) == ""


def test_no_negative_word_budget():
    """A cap below the headroom must not ask for a negative number of words."""
    for cap in (1, 10, 49, 51):
        assert builder.length_hint(cap, has_ws=True) == ""


def test_reason_given_matches_whether_state_is_tracked():
    assert "state block" in builder.length_hint(800, has_ws=True)
    assert "state block" not in builder.length_hint(800, has_ws=False)


# ----------------------------------------------------- in the assembled prompt

def test_hint_reaches_the_story_prompt(story):
    db, adventure, settings, _ = story
    settings.max_output_tokens = 800
    _, story_text, report = builder.build_context(adventure, settings)

    assert "506" in story_text
    labels = [s["label"] for s in report["sections"]]
    assert "length_hint" in labels


def test_emit_reminder_keeps_the_last_word(story):
    """The hint sits above the emit reminder: the reminder's whole value is the
    recency slot, and the model has to write the narration before the block."""
    db, adventure, settings, scenario_id = story
    with_schema(db, scenario_id, adventure)
    settings.max_output_tokens = 800

    _, story_text, report = builder.build_context(adventure, settings)

    assert story_text.rstrip().endswith(worldstate.EMIT_REMINDER.rstrip())
    labels = [s["label"] for s in report["sections"]]
    assert labels.index("length_hint") < labels.index("world_state_reminder")


def test_prompt_stays_inside_the_budget_on_a_long_story(story):
    """Regression guard: the hint is appended after history has already
    spent the budget, so it must be reserved up front like `EMIT_REMINDER`
    is.

    This check is weak on purpose. The history loop stops before crossing
    its budget, so it leaves about one action of slack, and the roughly
    30-token hint fits inside that slack. This catches a hint that grows
    large, not a missing reservation. The reservation itself is not
    observable from the outside."""
    db, adventure, settings, _ = story
    for i in range(4, 120):
        db.add(models.Action(
            adventure_id=adventure.id, type="ai" if i % 2 else "do",
            text=f"[{i}] " + "The road bends past the burnt mill and the smoke. " * 12,
        ))
    db.commit()
    db.expire_all()
    adventure = db.get(models.Adventure, adventure.id)

    settings.max_output_tokens = 2400
    settings.context_token_budget = 2048

    _, _, report = builder.build_context(adventure, settings)
    assert report["history"]["included"] < 120, "budget was never actually filled"
    assert report["tokens"]["total"] <= report["tokens"]["budget"]


def test_hint_is_counted_in_the_reported_totals(story):
    """Insights reports what the turn actually costs. A section that
    reaches the model but not the accounting makes that reported cost
    inaccurate."""
    db, adventure, settings, _ = story
    settings.max_output_tokens = 800

    _, _, report = builder.build_context(adventure, settings)
    hint = next(s for s in report["sections"] if s["label"] == "length_hint")
    assert hint["tokens"] > 0
    assert hint["text"] in report["prompt"]["story"]
    assert builder.count_tokens(report["prompt"]["story"]) <= report["tokens"]["total"]


def test_no_hint_section_when_the_cap_is_tiny(story):
    """An empty hint drops out entirely rather than leaving a blank section."""
    db, adventure, settings, _ = story
    settings.max_output_tokens = 60

    _, story_text, report = builder.build_context(adventure, settings)
    assert "length_hint" not in [s["label"] for s in report["sections"]]
    assert "Keep this turn" not in story_text
