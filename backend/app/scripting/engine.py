"""AI Dungeon-compatible script execution in an embedded QuickJS sandbox.

Each hook run is fully isolated (fresh Context), capped at 16 MB memory and
2 seconds CPU, with no filesystem/network/process access (QuickJS has none by
default). Scripts follow the AI Dungeon contract: define a `modifier(text)`
and call it as the last line; its return value `{ text, stop }` is the result.
"""

import json
from dataclasses import dataclass, field

import quickjs

MEMORY_LIMIT = 16 * 1024 * 1024
TIME_LIMIT_SECONDS = 2
HISTORY_WINDOW = 100  # recent actions exposed as `history`

# Globals per the official docs: text, state, history, storyCards, info,
# log/console.log, story card functions, plus legacy worldInfo aliases.
PRELUDE = """
"use strict";
var __logs = [];
var state = __DATA__.state;
var text = __DATA__.text;
var history = __DATA__.history;
var storyCards = __DATA__.storyCards;
var info = __DATA__.info;

function log(msg) {
  __logs.push(typeof msg === "string" ? msg : JSON.stringify(msg));
}
var console = { log: log };

// Returns the new card's index, or false if a card with those keys exists —
// matching real AI Dungeon. Note index 0 is falsy; that quirk is upstream's.
function addStoryCard(keys, entry, type) {
  for (var i = 0; i < storyCards.length; i++) {
    if (storyCards[i].keys === keys) return false;
  }
  storyCards.push({ id: null, keys: keys || "", entry: entry || "", type: type || "" });
  return storyCards.length - 1;
}
function updateStoryCard(index, keys, entry, type) {
  var card = storyCards[index];
  if (!card) throw new Error("Story card not found");
  card.keys = keys;
  card.entry = entry;
  card.type = type;
}
function removeStoryCard(index) {
  if (!storyCards[index]) throw new Error("Story card not found");
  storyCards.splice(index, 1);
}

// Legacy aliases used by older AI Dungeon scripts.
var worldInfo = storyCards;
var worldEntries = storyCards;
function addWorldEntry(keys, entry) { return addStoryCard(keys, entry, ""); }
function updateWorldEntry(index, keys, entry) {
  var card = storyCards[index];
  if (!card) throw new Error("World entry not found");
  card.keys = keys;
  card.entry = entry;
}
function removeWorldEntry(index) { return removeStoryCard(index); }
"""

COLLECT = """
JSON.stringify({
  result: (typeof __result === "undefined" || __result === null) ? null : __result,
  state: state,
  storyCards: storyCards,
  logs: __logs
})
"""


@dataclass
class HookResult:
    text: str
    stop: bool = False
    state: dict = field(default_factory=dict)
    story_cards: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    error: str | None = None


def run_hook(
    library_js: str,
    hook_js: str,
    text: str,
    state: dict,
    history: list[dict],
    story_cards: list[dict],
    info: dict,
) -> HookResult:
    """Run one modifier hook. Never raises: failures come back as .error with
    text/state/cards unchanged, so a bad script can't break a turn."""
    unchanged = HookResult(text=text, state=state, story_cards=story_cards)
    source = f"{library_js}\n;\n{hook_js}" if library_js.strip() else hook_js
    if not source.strip():
        return unchanged

    data = {
        "state": state,
        "text": text,
        "history": history[-HISTORY_WINDOW:],
        "storyCards": story_cards,
        "info": info,
    }
    try:
        ctx = quickjs.Context()
        ctx.set_memory_limit(MEMORY_LIMIT)
        ctx.set_time_limit(TIME_LIMIT_SECONDS)
        ctx.eval(f"var __DATA__ = {json.dumps(data)};")
        ctx.eval(PRELUDE)
        ctx.eval(f"var __SRC__ = {json.dumps(source)};")
        # Indirect eval keeps the script in global scope, so `modifier(text)` as the
        # script's final expression statement becomes the completion value.
        ctx.eval("var __result = (0, eval)(__SRC__);")
        collected = json.loads(ctx.eval(COLLECT))
    except quickjs.JSException as exc:
        unchanged.error = f"Script error: {exc}"
        return unchanged
    except Exception as exc:  # memory limit, invalid JSON state, engine faults
        unchanged.error = f"Script execution failed: {exc}"
        return unchanged

    result = collected.get("result")
    new_text, stop = text, False
    if isinstance(result, dict):
        if isinstance(result.get("text"), str):
            new_text = result["text"]
        stop = bool(result.get("stop"))
    elif isinstance(result, str):
        new_text = result

    new_state = collected.get("state")
    return HookResult(
        text=new_text,
        stop=stop,
        state=new_state if isinstance(new_state, dict) else {},
        story_cards=collected.get("storyCards") or [],
        logs=collected.get("logs") or [],
    )
