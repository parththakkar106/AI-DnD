# World-state refusals: what the engine throws away, and who gets told

Read this before you play the Pokémon demo again. It records why two bugs in
`plan/15-pokemon-demo-handover.md` were diagnosed wrongly, what the engine was
actually doing, and what changed. Everything here is merged and green. It **has** now been driven in a browser;
read "Driven in a browser" at the end first, because three of the five changes
did not work and one bug explains all three.

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

532 backend tests pass and the frontend builds. **The UI work has no automated
cover** — this project has no frontend test runner, which is the standing
reason its UI bugs are found by hand.

Run the backend from `backend/` with
`.venv/Scripts/python.exe -m pytest tests/`. The new file is
`tests/test_change_visibility.py` (23 tests). Each of the three mechanisms fails
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

---

## Driven in a browser, 2026-08-28

Adventure 45, four turns, on production against the demo model. Two of the five
changes worked. Three did not reach anyone, for one reason.

### The bug: the stored column dropped two of the three lists

`world_delta_of` in `routers/adventures.py` wrote `delta` and `applied` only.
Every consumer that distinguishes outcomes reads the other two:

- `Action.world_changes` builds `clamped_paths` from `world_delta["clamped"]`,
  so every chip carried `clamped: false`. `Play.jsx`'s `blocked = c.clamped &&
  d === 0` could never be true, and `(limited)` could never render.
- With no `rejected` list, a `kind: "rejected"` chip was unreachable.
- `worldstate.refusals` reads both lists, so `render_refusals` always returned
  an empty string and no correction ever reached the next prompt.

The `fix` string survived only because the engine stores it inside the `applied`
entry. Turn 3 is the record: the snapshot's `report.clamped` held
`npc.ivysaur.hp` and `npc.milo.active_hp` with correct `fix` text, both chips
came back `clamped: false`, turn 4's prompt contained no correction, and the
model repeated the same mistake.

`world_delta_of` now carries all three lists.

**Why the 21 tests passed.** The `action()` helper in
`test_change_visibility.py` built the column by hand as `{"delta": delta,
**report}`, with every list present. The write path was never exercised. The
helper now fills the column through `world_delta_of`. Removing the two lines
again fails 9 of the 23 tests; that was checked, not assumed.

### The two that were not code faults

`graveler_defeated` never fired and `world.turn` never moved. Both instructions
are present and correct in the assembled prompt: the goals line reads `Goals
(mark with milestones.<id>): graveler_defeated — …`, and the scenario says to
add 1 to `world.turn` every reply. The demo model ignores both. Change 3
landed; the model is the limit.

### The absolutes were coming from the scenario's own wording

The model sent a total, not a change, for almost every number:
`npc.ivysaur.hp: 96`, `npc.milo.active_hp: 65` then `88`, `player.potions: 2`.
Because every `hp` has `initial == max`, each one clamped back to where it
started. `potions` went **up** when the player spent one.

`EMIT_RULE` does say "CHANGES ONLY, as deltas (not new totals)", and
`EMIT_REMINDER` repeats "deltas only". The scenario contradicted both at closer
range. `milo.active_hp.desc` said "**Reset this to** the newcomer's full HP",
which is an instruction to send an absolute, and that desc is injected every
turn. The bullets said "**Drop** the HP … and **raise** it", naming a direction
but never a sign. The only line that said "(not a delta)" was
`player.active_pokemon`, so naming the exception made the rule look optional.

The one stat whose desc used delta wording, `pokemon_fainted` ("Add 1 each
time"), is the one that worked.

Fixed in `05-league-championship.json`: every `hp` desc and the potion desc now
state the sign, the bullets do too, and the lead-in says plainly that every
number is a change with a worked example. `milo.active_hp.max_delta_per_turn`
went 65 → 98, because a switch legitimately moves that stat a full bar and the
old cap made the reset unreachable in one turn.

**Not fixed:** the `initial == max` shape itself. It is now loud rather than
silent, and the wording removes the usual cause, but the shape is still there.

---

## Driven against Claude, 2026-08-28

The four turns above ran on the free demo model, which ignored two instructions
that were present and correct in the prompt. To separate model behavior from
code, the same demo was played again through `backend/tools/claude_shim.py`, an
OpenAI-compatible endpoint backed by the local `claude` command line tool. The
model was `sonnet`. See the README for how to run it.

Every one of the five changes worked:

| Turn | Chips |
|---|---|
| 1 | `turn +1`, `active pokemon → Wartortle`, `wartortle hp -8`, `milo active hp -46`, dashed `type advantage used refused — no such flag` |
| 2 | `turn +1`, `milo active hp -52`, `milo pokemon fainted +1`, `milo active pokemon → Onix`, `✓ graveler defeated` |
| 3 | `turn +1 (limited)`, dashed `milo active hp no change — at its limit` |
| 4 | `turn +1`, `milo active hp +32`, `✓ type advantage used` |

`graveler_defeated` fired for the first time in five playtests, `world.turn`
moved every turn, `pokemon_fainted` went 0 to 1 at the faint, and every HP number
was a signed change rather than a total. The two failures left open above were
the demo model, not the instructions.

**The refusal loop is verified end to end.** Turn 3 clamped to nothing. Turn 4's
assembled prompt, read from `GET /api/adventures/1/actions/9/context`, carried
the correction verbatim:

```
[Part of your last state block was not applied. Correct it in this turn's block:
- `npc.milo.active_hp` did not move. It is already at its minimum of 0 (it runs
  from 0 to 98; it moves at most 98 per turn).]
```

The model's next delta was `{"world.turn": 1, "npc.milo.active_hp": 32,
"milestones.type_advantage_used": true}`. That is the loop the whole change
exists for, and it had never been observed running.

### The one bug that is still a schema fault

At the faint the model set `npc.milo.active_pokemon` to `Onix` and sent no
positive `active_hp` change, so Onix arrived on the field at 0 of 98 and every
later hit was refused. Sonnet followed the rest of the scenario closely, so this
is the schema rather than the model: `active_hp` is one stat with one `max` of
98, shared by Graveler at 98, Onix at 90, and Kabutops at 88. A switch has to
raise it, and nothing in the schema can raise it on the referee's side.

The fix is to give Milo's three Pokemon their own NPC entries, which also removes
the `initial == max` shape from this demo. Not done: the session's remaining time
went to the guest starter instead.

### Cost

About $0.04 a turn, billed against the Claude subscription rather than a card.
Roughly 13k of each request's 20k prompt tokens is the command line tool's own
overhead; the app's prompt is about 7k.

---

## What shipped alongside it, 2026-08-28

**A permanent local test rig.** `backend/tools/claude_shim.py` is now in the
repo. It finds the `claude` binary through `AIDND_CLAUDE_BIN`, then `PATH`, then
the per-user install, and takes `--port` and `--claude`.

**The demo says Pokemon in its title, and has a Pokeball for cover art.**
`05-league-championship.json` is now `[Demo] Pokemon League Championship: Round
One`. Renaming a seed exposed a fault: `seed.py` matches a seed to its row by
title, so a rename inserted a second public scenario and stranded the first,
which is the same failure this file records for "Road to the Champion". Seeds
now carry `previous_titles`, and `_find_renamed` lands the rename on the
existing row. The scenario kept its id, and no orphan appeared.

The cover art is a 3.2 kB PNG data URI. An SVG one was tried first and stored as
an empty `image_url`: `app/images.py` accepts raster formats only, on purpose,
because SVG can carry script and these bytes are served from the app's own
origin. `backend/tools/make_pokeball.py` draws the ball with `zlib` alone, so
regenerating it needs no image library.

**Every new guest gets the played adventure.** `app/starter.py` copies a shipped
export bundle into each new guest account, from the guest mint in
`routers/auth.py`. The bundle is this session's adventure trimmed to its first
two exchanges, which ends on the knockout and shows an applied change, a refused
one, and a milestone. It stops before the Onix bug above, and the state it
leaves has Onix at its own 90 HP so a guest can play on from it.

The row building that `POST /adventures/import` did inline moved into
`bundle.materialize`, which both callers now use. The limit and rate checks
stayed in the endpoint: the starter writes a file the server ships, so it has no
untrusted list to cap. `tests/test_starter_adventure.py` covers the file, the
copy, the chips, the playable end state, and the two failure paths.
