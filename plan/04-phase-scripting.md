# Phase 4 â€” Scripting (AI Dungeon-compatible JavaScript)

**Goal:** real AI Dungeon scripts import and run: the three modifier hooks, persistent `state`,
and the scripting API surface.

## JS runtime

- [x] Embed a JS engine in Python: **quickjs** (preferred; check Windows wheel availability at
      implementation time; fallback: py-mini-racer, or Node subprocess as last resort).
- [x] Sandbox matching AI Dungeon's documented limits: each hook runs **isolated**, **16 MB
      memory cap**, **2-second timeout**; no filesystem/network/process access; script errors
      captured and surfaced in the UI, never crash a turn.

## AI Dungeon scripting model (compatibility target)

*(Per official docs: help.aidungeon.com/faq/how-do-i-write-scripts-and-use-scripting)*

Three lifecycle hooks â€” `onInput`, `onModelContext`, `onOutput`. Each script defines a modifier
and **must call it as its last line**:

```javascript
const modifier = (text) => {
  // script logic
  return { text, stop }
}
modifier(text)
```

- **onInput** â€” modifies player input before context construction.
- **onModelContext** â€” modifies the assembled text sent to the model.
- **onOutput** â€” modifies the model output before it is shown/stored.
- **Shared Library** â€” code prepended to all three slots.

Return contract:
- [x] `{ text, stop }`; `stop: true` from onInput prevents the AI call.
- [x] Empty-string `text` from onInput/onOutput â†’ user-facing error (replicate this behavior).

Globals provided (exact names from docs):

- [x] `text` â€” hook input (player input / context / AI response respectively).
- [x] `state` â€” persisted per adventure across turns (`Adventure.script_state`); includes
      `state.memory`, `state.message` (shown as a UI notice), `state.placeholders`.
- [x] `state.memory` slots: `context` (prepended to context), `authorsNote` (near end, before
      latest response), `frontMemory` (inserted right after the player's input).
- [x] `history` â€” array of recent actions: `{ text, rawText, type }`.
- [x] `storyCards` â€” array of `{ id, keys, entry, type }`, backed by the adventure's story cards.
- [x] Story card functions: `addStoryCard(keys, entry, type)` â†’ index (or `false` on duplicate),
      `updateStoryCard(index, keys, entry, type)` and `removeStoryCard(index)` â†’ throw if absent.
- [x] Legacy aliases for older scripts: `worldInfo` / `worldEntries`, `addWorldEntry`,
      `updateWorldEntry`, `removeWorldEntry` mapped onto the storyCards implementation.
- [x] `info` â€” `{ actionCount, characterNames, memoryLength, maxChars }`.
- [x] `log(message)` / `console.log` â€” captured per turn, shown in a script log panel.

## Pipeline integration

```
player input â†’ INPUT modifier â†’ format & store
context build â†’ CONTEXT modifier â†’ (snapshot includes pre- and post-script versions in Insights)
AI response â†’ OUTPUT modifier â†’ store & render
```

- [x] Insights (Phase 3) extended: show context before vs after the context modifier (diff view),
      and script log output per turn.

## Script management UI

- [x] Scripts page: create/edit scripts with a code editor (CodeMirror), one tab per slot
      (Library / Input / Context / Output), description field.
- [x] Attach scripts to scenarios; adventures inherit at creation. Enable/disable per adventure.
- [x] Test-run a script against sample text without an AI call.

## Import / Export

- [x] **Scripts**: export/import as JSON bundle `{ name, library, input, context, output }` and
      as raw `.js` files per slot (matching how AI Dungeon scripts circulate â€” paste or file).
- [x] **Scenarios**: export/import JSON including prompt, memory, author's note, story cards,
      and attached scripts. Accept AI Dungeon scenario export JSON where format is known;
      map fields best-effort and report anything unmapped.

## Exit criteria

Paste a real AI Dungeon script (e.g. a simple input modifier + state counter + world entry
manipulation) and it runs unmodified across turns; state persists; export a scenario with scripts,
re-import it into a fresh database, and play it.
