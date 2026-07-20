# Plan: undo/retry state revert (+ concurrency lock)

Fixes three linked issues around `script_state` (the shared per-adventure
"scoreboard" scripts write to) and undo/retry.

## Background

- `script_state` is one shared dict on `Adventure` (`models.py:90`), mutated in
  exactly ONE place: `pipeline.py:89` (`self.adventure.script_state = state`).
- Today, undo (`adventures.py:445`) and retry (`:422`) delete *actions* but never
  touch `script_state`, so state never rolls back.

## Issue 1 — retry double-applies state (pre-existing bug)

Retry deletes the last AI action and regenerates. The output hook already mutated
`script_state` on the first attempt; regenerating runs it again, stacking the change
(e.g. "add 10 gold" → 20 gold after one retry). Same root cause as undo not
reverting.

## Issue 4 — undo has no concurrency guard

Turns take `acquire_turn_lock` (`:189`); undo does not, so undo can race a turn
that is still streaming.

## Issue 2 — Memory Bank leftovers (smaller than expected)

`run_post_turn` already clamps `memory_cursor`/`summary_cursor` down to the current
action count (`memorybank.py:178-182`), so there is NO cursor stall. The only
remainder: a `Memory` created from a turn that was later undone stays behind, its
`source_start/source_end` now pointing past the end of the story.

---

## The fix

### 1. Snapshot state per turn  (Issue 1 + enables undo revert)

- Add column `state_before: JSON nullable` to `Action` (`models.py`).
- Migration: append `(25, "ALTER TABLE actions ADD COLUMN state_before JSON")`
  to `migrations.py`. `JSON` is valid on both SQLite and Postgres.
- In `run_player_turn` / `generate_turn`, when the FIRST action of a turn is
  created, stash `copy.deepcopy(adventure.script_state)` onto it — captured
  *before* any hook runs. (Player action for do/say/story; the AI action for a
  bare `continue`.)
- Fix retry directly: before regenerating, restore
  `adventure.script_state` from the deleted AI action's `state_before` so the
  output hook starts from the pre-turn scoreboard instead of the mutated one.

### 2. Revert on undo  (Issue depends on #1)

- In `undo_turn`, after deleting the popped actions, set
  `adventure.script_state = <deleted player/first action>.state_before` (fall back
  to `{}` if null, i.e. pre-migration turns), then commit.
- Only wire this into undo + retry — NOT the arbitrary
  `delete_action` endpoint (`:830`); mid-history state revert is undefined.

### 3. Lock undo  (Issue 4)

- Wrap `undo_turn` body in `acquire_turn_lock(adventure_id)` /
  `_active_turns.discard(...)` in a `finally`. It's synchronous (not SSE), so no
  `with_turn_lock` wrapper needed — just acquire and discard.

### 4. Clean up dangling memories  (Issue 2, optional)

- In `undo_turn` after deleting actions, delete any `Memory` whose `source_start`
  is >= the new story-action count (i.e. summarized a turn that no longer exists).
  Cursors already self-heal, so this is polish, not correctness.

## Known limits (document, don't fix)

- Story cards a script created (`_apply_cards`, `pipeline.py:87`) are NOT reverted —
  only text + script_state roll back.
- Pre-migration turns have `state_before = NULL` → undo falls back to `{}`.
- Demo turn cap is not refunded on undo (intentional).

## Test checklist

- Script that increments a counter: play → undo → counter back to prior value.
- Same script: play → retry → counter changes once, not twice.
- Undo during an active stream returns 409, doesn't corrupt state.
- Undo a turn old enough to have been summarized: no orphaned memory left.
