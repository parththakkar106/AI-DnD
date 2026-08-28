# World-state refusals: what the engine throws away, and who gets told

Read this before you play the Pokémon demo again. It records why two bugs in
`plan/15-pokemon-demo-handover.md` were diagnosed wrongly, what the engine was
actually doing, and what changed. Everything here is merged and green. **None of
it has been driven in a browser.**

**Last updated: 2026-08-28.**

---

## The one sentence version

`apply_delta` records three outcomes for every change the model sends —
`applied`, `clamped`, `rejected` — and everything downstream read only
`applied`. A refused change therefore reached the player as an ordinary chip and
reached the model, on the next turn, as a change that had succeeded.

## What was actually wrong

`plan/15` recorded two bugs and named a cause for each. Both causes were wrong,
and the investigation is worth keeping because the same reasoning trap is easy
to repeat: **the visible evidence was "the number did not move", and the natural
reading of that is that the model never tried.**

### `pokemon_left` was emitted every time

Adventure 43's action rows carry `milo pokemon_left` in `world_changes` at both
faints, each with `"delta": 0, "value": 3`. The model saw the faint and wrote
the path. It was not forgetting anything.

`old == new == 3` is reachable only from a **positive** value. `pokemon_left`
was `min 0, max 3, initial 3, max_delta_per_turn 1`, so `+2` capped to `+1`,
reached 4, and clamped back to the ceiling of 3. Net zero.

So the model sent the remaining count as an absolute — "two left" — instead of
a delta of `-1`. Milo's four stats alternate between the two conventions:

| stat | convention |
|---|---|
| `active_pokemon` | text, absolute |
| `active_hp` | number, delta |
| `active_status` | text, absolute |
| `pokemon_left` | number, delta |

HP survives because damage is naturally phrased as a change. A count is
naturally phrased as a state, so it got text semantics.

**The general rule this produces:** a numeric stat whose `initial` equals the
boundary it moves away from turns every wrong-signed change into a silent
no-op. Every `hp` in the scenario has that shape (`initial == max`). It has
never fired only because damage is phrased as a decrease by luck of language.

### Milestones were never emitted at all

Zero milestone changes across nine AI turns. `EMIT_RULE` asks for
`"milestones.<id>": true` and `apply_delta` matches `<id>` against the schema
key, but `render_state_section` printed only the description, and
`render_reference` skipped the milestones section entirely
(`STAT_SECTIONS = ("world", "player")`). The string `graveler_defeated` was
nowhere in the prompt.

The same playtest is its own control: `sandstorm_active` is a flag, flags
*are* printed by name, and it worked.

### The replay was teaching the model to repeat itself

Found while deciding whether to feed refusals forward, and the most damaging of
the three. `_history_text` re-attached each past turn's state block from
`world_delta["delta"]` — **what the model sent**, not what was applied. So the
turn after the faint contained the model's own block claiming
`"npc.milo.pokemon_left": 2`, directly above a live values line reading
`pokemon_left 3/3`, with nothing to say which was true.

That is a per-turn lesson that sending `2` is correct. The identical mistake at
the second faint is what that lesson predicts.

## What changed

Five changes, on `fix-silent-clamps-and-milestone-ids`.

1. **`Action.world_changes` reports refusals** (`models.py`). Reads `clamped`
   and `rejected` beside `applied`. Accepted stats carry `clamped`; refusals
   become `kind: "rejected"` entries. The `fix` key is present only when the
   engine wrote one — it is empty for every accepted change, and this property
   runs for every action of every list response.
2. **The UI distinguishes three outcomes** (`Play.jsx`, `index.css`). Clamped to
   a standstill reads `no change — at its limit` on a dashed chip; a partial
   clamp is marked `(limited)`; a rejection carries its reason. Dashed and
   dimmed rather than red: the rules refusing a change is them working.
3. **Milestones are named to the model** (`engine.py`). The goals line is now
   `Goals (mark with milestones.<id>): graveler_defeated — Knock out Milo's lead
   Graveler; …`, the same treatment NPCs get with `(npc.milo)`.
4. **Refusals carry a generated correction** (`engine.py`). Each rejection, and
   each clamp that moved nothing, builds a `fix` string from the stat definition
   at the point of refusal, so it quotes the real limits and lists the real
   names. `render_refusals()` renders them into the prompt directly above
   `EMIT_REMINDER`, for the previous AI turn only.
5. **History replays what was accepted** (`builder.py`). `applied_delta()`
   rebuilds the block from `report["applied"]`, dropping any numeric entry where
   `new == old` so a change that moved nothing cannot be copied as a zero.

Plus the demo scenario: `pokemon_left` became `pokemon_fainted`
(`type: counter`, `initial: 0`), which puts a wrong sign on the counter rule
where it is rejected out loud instead of absorbed. The faint instruction moved
into its own paragraph, and a `world.turn` line was added — it sat at 0 for the
whole playtest because nothing ever told the model to move it.

### The design call worth not re-litigating

**A clamp that reduced a change but still moved the value says nothing.** Only
total losses are reported. If you tell a model its 80 damage became 30, it can
treat the shortfall as a debt and send the remaining 50 next turn — which is the
swing `max_delta_per_turn` exists to prevent. A rejection has no partial credit
to chase. `test_a_clamp_that_still_moved_the_value_says_nothing` pins this.

## How to test it

530 backend tests pass and the frontend builds. **The whole of the UI work is
unverified** — this project has no frontend test runner, which is the standing
reason its UI bugs are found by hand.

Run the backend from `backend/` with
`.venv/Scripts/python.exe -m pytest tests/`. The new file is
`tests/test_change_visibility.py` (21 tests). Each of the three mechanisms fails
its own test when disabled; that was checked by sabotage, not assumed.

To drive it, re-seed the scenario and play the demo:

1. **A refusal chip.** Open the World State drawer, use its ✎ edit mode to put
   Milo's `active_hp` at full, then play a turn where he takes no damage but the
   model tries to heal him. Easier and more reliable: send a deliberately wrong
   block by editing an AI turn. What you are looking for is a dashed chip
   reading `no change — at its limit`, not a `+0`.
2. **The milestone.** Knock out Graveler. `graveler_defeated` should tick, and
   a `✓ graveler defeated` chip should appear. This is the single clearest
   pass/fail in the whole change — it never once happened before.
3. **The faint counter.** At the same faint, `pokemon_fainted` should go 0 → 1.
   If the model sends an absolute again, it is now refused rather than absorbed,
   and the refusal note should appear in the *next* turn's prompt. Read it under
   Insights → the turn's context snapshot, section `world_state_refusals`.
4. **The replay.** In the same snapshot, check the replayed history: a past
   turn's `state` block should carry only the changes that were accepted.
5. **`world.turn`** should now advance by 1 per reply.

Check the narrow layout too. The chips grew longer text, and `.chg` is inside
the story column with nothing to scroll sideways — `overflow-wrap: anywhere` is
doing the work, and it was not re-checked at 390 px. Chrome clamps its minimum
window width to ~500 px, so relaunch with `--window-size=` rather than trying to
resize a maximized window.

## Still open

- **The missing `state` block from `plan/15` bug 1 is unexplained.** Truncation
  at `max_output_tokens` removes the block, which `LENGTH_HEADROOM` exists to
  prevent, and it was never ruled out. The distinguishing evidence is whether
  the narration ends mid-sentence with `finish_reason: length`.
- **Every `hp` stat still has the `initial == max` shape.** Now visible when it
  bites, rather than silent, but not designed out.
- **The stale "Road to the Champion" scenario and adventure 42** are still on
  production. Deleting them is hand-work and was deliberately not automated.
- **The Bandit Camp demo (`04-rpg-world-state.json`) was not checked** for the
  same milestone problem. Its milestones were equally unnamed to the model
  before this change, so it is worth asking whether one has ever fired there.
