# Phase 5 — Polish & quality of life

**Goal:** the app feels like AI Dungeon — cohesive dark UI, smooth flows, safe data handling.

## UI/UX pass

- [x] Theming: refined dark palette, serif story typography, subtle textures/gradients à la
      AI Dungeon; consistent buttons, panels, modals; responsive layout.
- [x] Home: adventure cards with scenario name, last-played time, action count; search/filter;
      scenario gallery with tags.
- [x] Play screen: collapsible right side panel (Memory / Cards / Scripts / Insights tabs),
      keyboard shortcuts (Enter send, Ctrl+Z undo, Ctrl+R retry), smooth streaming autoscroll
      that pauses when the user scrolls up.
- [x] Scenario **placeholders**: `${Character name}` style variables in prompt/memory prompt the
      player for values when starting an adventure (AI Dungeon behavior).

## Data & robustness

- [x] Adventure export/import (full JSON: actions, memory, cards, script state) — backup/share.
- [x] Delete confirmations (trash/soft-delete skipped — plain confirm dialogs).
- [x] SQLite migrations story (versioned schema bootstrap via PRAGMA user_version —
      `backend/app/migrations.py`).
- [x] Request logging + a debug page tailing recent provider requests/responses (bodies redacted
      of API key) — "Recent AI requests" on the Settings page.
- [x] Graceful handling: provider timeout/cancel (stop generation button), concurrent turn lock
      per adventure.

## Nice-to-haves (only if time/interest)

- [ ] Multiple provider profiles with quick switching (e.g. local Ollama vs OpenRouter).
- [ ] Per-scenario generation params overriding global settings.
- [ ] Retry with "give me something different" (temperature bump / anti-repeat nudge).
- [ ] Basic light theme toggle.

## Exit criteria

A friend could sit down at `localhost`, start a scenario with placeholders, play comfortably,
peek at Insights, and you can back up / restore everything via export files.
