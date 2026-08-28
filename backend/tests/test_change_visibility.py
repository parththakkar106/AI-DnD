"""Tests for the changes the engine refuses, and for naming a milestone.

A refused change used to leave no trace a player could see. The turn summary
read `world_delta["applied"]` alone, so a value the model pushed past its
ceiling came back with a delta of 0 and rendered as an ordinary chip. The
engine had recorded the refusal and nothing showed it.

The milestone half is the same shape. `apply_delta` matches a milestone by its
schema id, and the context named goals by description only, so the model had no
way to learn the id it was being asked to send.

    python -m pytest tests/test_change_visibility.py -v
"""
import json
import pathlib

from app import models
from app import worldstate as w

SEED = pathlib.Path(__file__).resolve().parents[1] / "app" / "seed_data"

SCHEMA = {
    "world": {"day": {"type": "counter", "min": 1, "initial": 1}},
    "player": {
        "hp": {"min": 0, "max": 100, "initial": 100, "max_delta_per_turn": 30},
        # Starts at its own ceiling and only ever falls, which is the shape that
        # turns a wrong-signed delta into a change of nothing.
        "arrows": {"min": 0, "max": 6, "initial": 6, "max_delta_per_turn": 1},
    },
    "npcs": {"gwen": {"name": "Gwen", "stats": {"trust": {"min": -100, "max": 100, "initial": 0}}}},
    "flags": {"has_key": {"desc": "Holds the key", "initial": False}},
    "milestones": {
        "rescue_gwen": {"desc": "Rescue Gwen"},
        "escape_keep": {"desc": "Escape the keep"},
    },
}


def action(delta, index=1):
    """Returns an unsaved Action carrying the report `apply_delta` produced."""
    ws, report = w.apply_delta(w.instantiate(SCHEMA), SCHEMA, delta, index)
    return models.Action(world_delta={"delta": delta, **report})


# --------------------------------------------------------------------------- #
# The goals line names the id the AI has to send
# --------------------------------------------------------------------------- #

def test_goals_line_names_the_milestone_id():
    line = w.render_state_section(w.instantiate(SCHEMA), SCHEMA, {})
    assert "rescue_gwen — Rescue Gwen" in line
    assert "escape_keep — Escape the keep" in line
    # The path is spelled out, because the id alone does not say how to send it.
    assert "milestones.<id>" in line


def test_achieved_line_names_the_id_too():
    ws, _ = w.apply_delta(w.instantiate(SCHEMA), SCHEMA, {"milestones.rescue_gwen": True}, 1)
    line = w.render_state_section(ws, SCHEMA, {})
    assert "Achieved: rescue_gwen — Rescue Gwen." in line
    # A reached milestone leaves the goals list.
    assert "rescue_gwen — Rescue Gwen;" not in line


def test_every_milestone_in_the_demo_is_named_to_the_model():
    """The demo that exposed this must not lose the ids again."""
    schema = json.loads((SEED / "05-league-championship.json").read_text(encoding="utf-8"))["stat_schema"]
    line = w.render_state_section(w.instantiate(schema), schema, {})
    for mid in schema["milestones"]:
        assert mid in line


# --------------------------------------------------------------------------- #
# A refused change reaches the turn summary
# --------------------------------------------------------------------------- #

def test_a_clamp_that_changes_nothing_is_still_reported():
    """The Pokémon bug: a positive delta on a stat already at its ceiling.

    `arrows` sits at 6 of a maximum 6, so `+2` caps to `+1`, reaches 7, and
    clamps back to 6. The value never moves, and the summary has to say so.
    """
    chips = action({"player.arrows": 2}).world_changes
    arrows = [c for c in chips if c["label"] == "arrows"]
    assert len(arrows) == 1
    assert arrows[0]["delta"] == 0
    assert arrows[0]["clamped"] is True


def test_a_clamp_that_still_moves_the_value_is_marked():
    chips = action({"player.hp": -80}).world_changes
    hp = [c for c in chips if c["label"] == "hp"][0]
    assert hp["delta"] == -30  # max_delta_per_turn
    assert hp["clamped"] is True


def test_an_accepted_change_is_not_marked():
    chips = action({"player.hp": -10}).world_changes
    hp = [c for c in chips if c["label"] == "hp"][0]
    assert hp["delta"] == -10
    assert hp["clamped"] is False


def test_a_refusal_becomes_its_own_entry_carrying_the_reason():
    chips = action({"world.day": -1, "player.stamina": 5}).world_changes
    refused = {c["label"]: c["reason"] for c in chips if c["kind"] == "rejected"}
    assert refused == {"day": "counter can't decrease", "stamina": "unknown stat"}


def test_a_refused_npc_stat_keeps_the_npc_in_its_label():
    chips = action({"npc.gwen.bogus": 5}).world_changes
    refused = [c for c in chips if c["kind"] == "rejected"][0]
    assert refused["label"] == "gwen bogus"


def test_accepted_and_refused_changes_appear_together():
    chips = action({"player.hp": -10, "world.day": -1}).world_changes
    kinds = [c["kind"] for c in chips]
    assert "stat" in kinds and "rejected" in kinds


def test_a_turn_that_changed_nothing_still_has_no_chips():
    assert models.Action(world_delta=None).world_changes == []


def test_flags_and_milestones_are_unchanged_by_the_new_fields():
    chips = action({"flags.has_key": True, "milestones.rescue_gwen": True}).world_changes
    assert {"kind": "flag", "label": "has_key", "on": True} in chips
    assert {"kind": "milestone", "label": "rescue_gwen"} in chips


# --------------------------------------------------------------------------- #
# The demo stat counts up, so a wrong sign is refused rather than absorbed
# --------------------------------------------------------------------------- #

def test_milos_faint_counter_counts_up_from_zero():
    """A stat that starts at its ceiling cannot report a wrong sign.

    `pokemon_left` began at 3 of a maximum 3, so the model sending the count it
    had left rather than a delta clamped to no change at all. Counting the
    faints up from 0 puts the wrong direction on the counter rule, which
    refuses it out loud.
    """
    schema = json.loads((SEED / "05-league-championship.json").read_text(encoding="utf-8"))["stat_schema"]
    stat = schema["npcs"]["milo"]["stats"]["pokemon_fainted"]
    assert stat["initial"] == 0 and stat["type"] == "counter"

    ws = w.instantiate(schema)
    after, report = w.apply_delta(ws, schema, {"npc.milo.pokemon_fainted": 1}, 1)
    assert after["npc"]["milo"]["pokemon_fainted"] == 1

    _, report = w.apply_delta(ws, schema, {"npc.milo.pokemon_fainted": -1}, 1)
    assert len(report["rejected"]) == 1
    refused = report["rejected"][0]
    assert refused["path"] == "npc.milo.pokemon_fainted"
    assert refused["reason"] == "counter can't decrease"


def test_the_demo_no_longer_mentions_the_old_stat():
    """The instructions name the stat, so a rename has to reach them too."""
    raw = (SEED / "05-league-championship.json").read_text(encoding="utf-8")
    assert "pokemon_left" not in raw
    assert "pokemon_fainted" in json.loads(raw)["ai_instructions"]


def test_the_demo_asks_for_the_turn_counter():
    """`world.turn` sat at 0 for a whole playtest: nothing told the model to
    move it. The schema defining a stat is not an instruction to update it."""
    d = json.loads((SEED / "05-league-championship.json").read_text(encoding="utf-8"))
    assert "world.turn" in d["stat_schema"]["world"] or "turn" in d["stat_schema"]["world"]
    assert "world.turn" in d["ai_instructions"]


# --------------------------------------------------------------------------- #
# What the model is told about its own refused changes
# --------------------------------------------------------------------------- #

def test_history_replays_what_was_accepted_not_what_was_sent():
    """The contradiction that taught the model to repeat itself.

    `arrows` is at its ceiling, so `+2` changes nothing. Replaying the sent
    delta showed the model a change the live values disagreed with.
    """
    from app.context.builder import _history_text

    a = action({"player.arrows": 2, "player.hp": -10})
    a.text = "The arrow flies."
    replayed = _history_text(a)
    assert '"player.hp": -10' in replayed
    assert "arrows" not in replayed


def test_history_replay_keeps_flags_and_milestones_and_text():
    from app.context.builder import _history_text

    a = action({"flags.has_key": True, "milestones.rescue_gwen": True})
    a.text = "The lock gives."
    replayed = _history_text(a)
    assert '"flags.has_key": true' in replayed
    assert '"milestones.rescue_gwen": true' in replayed


def test_a_refusal_reaches_the_model_with_the_valid_names():
    note = w.render_refusals(action({"milestones.bogus": True}).world_delta)
    assert "There is no milestone `bogus`" in note
    # The correction has to name what it could have sent instead.
    assert "`rescue_gwen`" in note and "`escape_keep`" in note


def test_a_wrong_sign_on_a_counter_is_explained():
    note = w.render_refusals(action({"world.day": -1}).world_delta)
    assert "only counts up" in note


def test_a_clamp_that_moved_nothing_quotes_the_limit():
    note = w.render_refusals(action({"player.arrows": 2}).world_delta)
    assert "did not move" in note
    assert "maximum of 6" in note and "it runs from 0 to 6" in note


def test_a_clamp_that_still_moved_the_value_says_nothing():
    """Reporting a trimmed change invites the model to send the remainder next
    turn, which is the swing `max_delta_per_turn` exists to prevent."""
    assert w.render_refusals(action({"player.hp": -80}).world_delta) == ""


def test_a_clean_turn_adds_no_note():
    assert w.render_refusals(action({"player.hp": -10}).world_delta) == ""
    assert w.render_refusals(None) == ""
