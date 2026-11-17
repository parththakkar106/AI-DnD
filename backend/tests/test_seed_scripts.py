"""Runs every seeded scenario's scripts in the real sandbox for 20 turns.

A hook error is swallowed in play, so a broken seed script fails silently. This
test fails instead. It also checks that a context hook leaves the state_delta
reminder as the last line, because weak models follow the text nearest their
reply.
"""
import json
import pathlib

import pytest

from app import worldstate
from app.scripting.engine import run_hook

SEEDS = sorted((pathlib.Path(__file__).parents[1] / "app" / "seed_data").glob("*.json"))
INPUTS = ["> You look around.", '> You say "Who was here?"', "A story line.",
          "> You accuse the butler.", "> You run for the door."]


@pytest.mark.parametrize("path", [p for p in SEEDS if json.loads(p.read_text("utf-8")).get("scripts")],
                         ids=lambda p: p.name)
def test_seed_scripts_run_clean(path):
    seed = json.loads(path.read_text("utf-8"))
    has_ws = bool(seed.get("stat_schema"))
    state_block = ("Current world state (running totals; report changes to these in your "
                   "state_delta block):\nYou: hp 10/10.\nFlags: none.")
    context = "\n\n".join(x for x in (
        "You are the narrator.", seed.get("ai_instructions", ""),
        "Plot essentials:\n" + seed.get("memory", ""), seed.get("prompt", ""),
        "> You look around.", state_block,
        "[Hard limit: this turn must not exceed 300 words.]",
        worldstate.EMIT_REMINDER if has_ws else "") if x)
    state: dict = {}
    for script in seed["scripts"]:
        lib = script.get("library_js", "")
        history = [{"text": seed.get("prompt", ""), "rawText": seed.get("prompt", ""), "type": "start"}]
        for turn in range(1, 21):
            info = {"actionCount": 2 * turn - 1, "characterNames": [],
                    "memoryLength": len(seed.get("memory", "")), "maxChars": 0}
            if script.get("input_js", "").strip():
                res = run_hook(lib, script["input_js"], INPUTS[turn % len(INPUTS)],
                               state, history, [], info)
                assert res.error is None, f"T{turn} input: {res.error}"
                assert res.text.strip(), f"T{turn} input hook returned empty text"
                state = res.state
            info["actionCount"] += 1
            if script.get("context_js", "").strip():
                res = run_hook(lib, script["context_js"], context, state, history, [], info)
                assert res.error is None, f"T{turn} context: {res.error}"
                if has_ws:
                    assert res.text.rstrip().endswith(worldstate.EMIT_REMINDER.strip()), \
                        f"T{turn} context hook put text after the state_delta reminder"
                state = res.state
