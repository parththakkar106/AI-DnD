# Phase 3 â€” Context engine + Insights

**Goal:** AI Dungeon-grade context management, and full visibility into every prompt.

## Context assembly (`context/builder.py`)

Assembles the prompt each turn from AI Dungeon's **plot components**
(per help.aidungeon.com/faq/the-memory-system):

```
[AI Instructions]               â† behavioral guidance for the model (always included)
[Plot Essentials]               â† key facts for constant recall â€” the classic "Memory" (always)
[Story Summary]                 â† running summary slot; manual in this phase, auto in Phase 6
[Triggered Story Cards]         â† "World Lore: <entry>" for each triggered card (conditional)
[Story history]                 â† as many recent actions as fit the token budget
[Author's Note]                 â† injected N lines (default 3) before the end of history
[Latest player action]          â† (+ script frontMemory right after it, Phase 4)
```

- [x] **AI Instructions / Plot Essentials / Story Summary / Author's Note**: adventure-level
      free-text fields, always included, editable mid-adventure from the side panel.
- [x] **Story cards** â€” five fields per official docs: **Type** (organizational, not sent to AI),
      **Name** (not sent to AI), **Entry** (sent when triggered), **Triggers**, **Notes** (not sent).
  - Triggers: comma-separated words/phrases; **case-insensitive but space-sensitive**;
    **partial-word matching** (`boat` triggers on `boats`); matched against both player input
    and AI output in the recent-story window.
  - Not instant: a card triggered mid-response only enters context on the *next* turn; once
    triggered, stays active while the triggering text remains in the context window.
  - Triggered entries injected once each, prefixed `World Lore:`; story cards are the **first
    component dropped** when context is full.
  - Editable per-adventure (copied from scenario at creation). Soft-cap sanity limit (AI Dungeon
    allows 5,000/adventure).
- [x] **Author's Note**: inserted near the end (strongest steering position), formatted
      `[Author's note: <text>]`.
- [x] **Token budgeting** with tiktoken: always-included components (AI Instructions, Plot
      Essentials, Story Summary, Author's Note) reserved first; story cards get a capped share
      and are dropped first when over budget; remainder goes to story history (newest first).
      Budget = `context_token_budget` setting.
- [x] Slots for script-provided memory overrides (Phase 4): `state.memory.context` (prepended),
      `state.memory.authorsNote` (replaces/augments author's note), `state.memory.frontMemory`
      (inserted immediately after the latest player action).
- [x] Builder returns a structured `ContextReport`: ordered sections, each with source label,
      text, token count; plus totals and a list of triggered cards (and which keyword fired).

## Insights

- [x] Every AI turn stores its `ContextReport` on the `Action` row (`context_snapshot`).
- [x] `GET /adventures/{id}/actions/{id}/context` returns it.
- [x] **Insights panel** in Play UI (drawer/tab):
  - Exact final prompt text as sent, sectioned and color-coded (memory / world info / history /
    author's note / input), with per-section token counts and total vs budget.
  - Which story cards triggered and on which keyword; which history got cut off.
  - Viewable for the *upcoming* turn (dry-run endpoint: "what would be sent now") and for any
    past AI action.
- [x] Adventure side panel: edit memory, author's note, story cards mid-game (AI Dungeon's
      right-hand panel equivalent).

## Exit criteria

Create a card with key `dragon`; mention a dragon in play and see the card enter the context in
the Insights panel (and influence the AI); verify memory and author's note appear in the snapshot
in the right positions; long adventures visibly trim oldest history within budget.
