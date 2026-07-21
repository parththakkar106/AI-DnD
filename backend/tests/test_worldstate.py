"""Unit tests for the RPG world-state engine (Phase 12): delta extraction and
the clamp/cooldown/milestone referee.

    python -m pytest tests/test_worldstate.py -v
"""
from app import worldstate as w

SCHEMA = {
    "world": {"day": {"type": "counter", "min": 1, "initial": 1}},
    "player": {
        "hp": {"min": 0, "max": 100, "initial": 100, "max_delta_per_turn": 30,
               "bands": [[0, 20, "very weak"], [20, 40, "hurt"],
                         [40, 60, "minor damage"], [60, 90, "healthy"],
                         [90, 100, "full health"]]},
    },
    "npc": {"trust": {"min": -100, "max": 100, "initial": 0, "cooldown": 2}},
    "flags": {
        "has_key": {"desc": "Holds the key", "initial": False},
        "disguised": {"desc": "In disguise"},
    },
    "milestones": {"rescue_gwen": {"desc": "Rescue Gwen"}},
}


def fresh():
    return w.instantiate(SCHEMA)


def test_instantiate_uses_initials():
    ws = fresh()
    assert ws["world"] == {"day": 1}
    assert ws["player"] == {"hp": 100}
    assert ws["npc"] == {} and ws["milestones"] == {}


def test_has_schema():
    assert w.has_schema(SCHEMA)
    assert not w.has_schema(None)
    assert not w.has_schema({})
    assert not w.has_schema({"npc_card_types": ["npc"]})  # config only, no stats


def test_max_delta_per_turn_clamps():
    ws, report = w.apply_delta(fresh(), SCHEMA, {"player.hp": -50}, 5)
    assert ws["player"]["hp"] == 70  # -50 capped to -30
    assert report["clamped"]


def test_clamp_to_min():
    ws, _ = w.apply_delta(fresh(), SCHEMA, {"player.hp": -30}, 1)
    ws, _ = w.apply_delta(ws, SCHEMA, {"player.hp": -30}, 3)
    ws, _ = w.apply_delta(ws, SCHEMA, {"player.hp": -30}, 5)
    ws, report = w.apply_delta(ws, SCHEMA, {"player.hp": -30}, 7)
    assert ws["player"]["hp"] == 0  # 100-30-30-30-30 clamps at min 0
    assert report["clamped"]


def test_counter_rejects_negative():
    ws, report = w.apply_delta(fresh(), SCHEMA, {"world.day": -1}, 3)
    assert ws["world"]["day"] == 1
    assert report["rejected"][0]["reason"] == "counter can't decrease"
    ws, _ = w.apply_delta(ws, SCHEMA, {"world.day": 1}, 4)
    assert ws["world"]["day"] == 2


def test_npc_lazy_init_and_cooldown():
    ws, _ = w.apply_delta(fresh(), SCHEMA, {"npc.12.trust": 10}, 7)
    assert ws["npc"]["12"]["trust"] == 10  # instantiated from template + applied
    # cooldown 2: another change at index 8 is too soon.
    ws, report = w.apply_delta(ws, SCHEMA, {"npc.12.trust": 10}, 8)
    assert ws["npc"]["12"]["trust"] == 10
    assert report["rejected"][0]["reason"] == "cooldown"
    # far enough later, it applies.
    ws, _ = w.apply_delta(ws, SCHEMA, {"npc.12.trust": 10}, 10)
    assert ws["npc"]["12"]["trust"] == 20


def test_milestone_sticky():
    ws, report = w.apply_delta(fresh(), SCHEMA, {"milestones.rescue_gwen": True}, 9)
    assert ws["milestones"]["rescue_gwen"] == {"reached": True, "at": 9}
    assert report["applied"]
    # second set is a silent no-op.
    ws, report = w.apply_delta(ws, SCHEMA, {"milestones.rescue_gwen": True}, 11)
    assert ws["milestones"]["rescue_gwen"]["at"] == 9
    assert not report["applied"]
    # false is ignored.
    ws, report = w.apply_delta(ws, SCHEMA, {"milestones.rescue_gwen": False}, 13)
    assert ws["milestones"]["rescue_gwen"]["reached"] is True


def test_flags_toggle_both_ways():
    ws = fresh()
    assert ws["flags"] == {"has_key": False, "disguised": False}  # initials
    ws, report = w.apply_delta(ws, SCHEMA, {"flags.has_key": True}, 1)
    assert ws["flags"]["has_key"] is True
    assert report["applied"]
    # flip back off — flags are two-way (unlike sticky milestones).
    ws, _ = w.apply_delta(ws, SCHEMA, {"flags.has_key": False}, 2)
    assert ws["flags"]["has_key"] is False
    # setting to the same value is a no-op.
    ws, report = w.apply_delta(ws, SCHEMA, {"flags.has_key": False}, 3)
    assert not report["applied"]


def test_flag_rejects_non_bool_and_unknown():
    ws, report = w.apply_delta(fresh(), SCHEMA, {"flags.has_key": 1, "flags.nope": True}, 1)
    reasons = {r["reason"] for r in report["rejected"]}
    assert reasons == {"not a boolean", "unknown flag"}
    assert ws["flags"]["has_key"] is False


def test_reference_includes_desc_and_bands_independently():
    guide = w.render_reference(SCHEMA)
    # hp has both a description and a band ladder.
    assert "very weak" in guide and "range 0–100" in guide
    # day (a counter here has no desc/bands) contributes nothing; flags show desc.
    assert "has_key (flag) — Holds the key." in guide


def test_unknown_paths_rejected_not_fatal():
    ws, report = w.apply_delta(fresh(), SCHEMA, {"player.stamina": -5, "bogus": 1}, 2)
    reasons = {r["reason"] for r in report["rejected"]}
    assert reasons == {"unknown stat", "unknown path"}
    assert ws["player"]["hp"] == 100  # untouched


def test_extract_fenced_delta_tolerates_mess():
    text = 'You strike.\n\n```state\n{"player.hp": -15, "npc.12.trust": +5,}\n```'
    clean, delta = w.extract_delta(text)
    assert clean == "You strike."
    assert delta == {"player.hp": -15, "npc.12.trust": 5}


def test_extract_no_block():
    clean, delta = w.extract_delta("Just prose that ends normally.")
    assert delta == {}
    assert clean == "Just prose that ends normally."


def test_extract_prose_ending_in_brace_not_eaten():
    # A bare object with no dotted keys is not a delta — leave the text alone.
    clean, delta = w.extract_delta('He said {this}')
    assert delta == {}
    assert clean == "He said {this}"


def test_band_label():
    d = SCHEMA["player"]["hp"]
    assert w.band_label(d, 10) == "very weak"
    assert w.band_label(d, 55) == "minor damage"
    assert w.band_label(d, 100) == "full health"  # inclusive top edge
