# Pokémon League Championship demo: playtest handover

Read this before you resume work on the demo scenario. It covers the current
state of `05-league-championship.json`, what a live 10-turn playtest on
production confirmed, and two bugs the playtest found.

**Last updated: 2026-08-28.**

> **Superseded in part on 2026-08-28.** Both bugs below were investigated against the
> real data and **both root causes named here are wrong**. See
> `plan/16-world-state-refusals.md` for what was actually happening and what was
> changed. The playtest record and the "Confirmed working" section still stand.

---

## Where things stand

The scenario is live at `https://ai-dnd-1gmp.onrender.com` as
**"[Demo] League Championship: Round One"**, seeded from
`backend/app/seed_data/05-league-championship.json`. It replaced an earlier,
weaker draft titled "Road to the Champion" — that old scenario and its stale
test adventure (adventure id 42) are still in the production database. Delete
them by hand from `/scenarios` and `/adventures` when convenient; a delete
click froze the browser tab during this session behind what looked like a
native confirm dialog, so budget time for that if you try again.

The schema nests all five of the player's Pokémon and Milo's Pokémon under
`npcs`, not flattened into `player.<name>_hp` fields. Each npc entry carries
its own `stats` map (`hp`, `status`, and for Milo, `active_pokemon`,
`active_hp`, `active_status`, `pokemon_left`). `player.active_pokemon` is a
text stat that names whichever of the player's Pokémon is currently out. This
design survived a real playtest: see "Confirmed working" below.

Settings on the test account now point at the user's own OpenRouter key,
endpoint `https://openrouter.ai/api/v1`, model `deepseek/deepseek-v4-flash-0731`,
reasoning budget `-1`. The shared demo key's model
(`google/gemma-4-26b-a4b-it:free`) was hitting persistent 429s from OpenRouter
capacity, not from the app's own rate limiter — switch back to it only after
confirming that model isn't still rate-limited.

## Confirmed working: a live 10-turn playtest

Adventure id 43, played turn by turn against DeepSeek V4 Flash on production.
Milo's Graveler and Onix both fainted; Kabutops came out third. Across ten
turns:

- HP tracked correctly on both sides, including sandstorm chip damage each
  turn once `sandstorm_active` flipped on.
- `status` stayed correctly independent per Pokémon (all `none` throughout
  this run — no status move was tried).
- `player.active_pokemon` and `npc.milo.active_pokemon` both switched
  correctly as Pokémon were sent out or fainted (Pidgeotto → Wartortle →
  Machoke; Graveler → Onix → Kabutops).
- `player.potions` decremented correctly on use (3 → 2) and the healed
  Pokémon's HP rose by the expected amount.
- The World State sidebar reflected every one of these changes live, without
  a manual refresh, after the model's reply finished streaming.

This confirms the nested-`npcs` redesign from earlier in the session was the
right fix for "why did you flatten then" — parallel entities with independent
stats work as npcs, not as flattened player fields.

## Two bugs the playtest found

**1. The model sometimes skips the trailing `state` block entirely.**

> **Unconfirmed.** Truncation at `max_output_tokens` removes the block too, and it
> was never ruled out here. See `plan/16`.
On the very first turn of this run, DeepSeek V4 Flash narrated a Wartortle HP
drop but never appended the ` ```state ` block the engine parses. The engine
correctly left the state untouched — this is model non-compliance, not an
engine bug — but the drop was silent: no error, no visible sign in the UI
beyond "the numbers didn't move." A `Retry` on that same turn produced the
delta correctly. Confirmed by reading the raw action row over
`/api/adventures/{id}/actions` — `hasState` came back `false` on the first
attempt and `true` on the retry. If this happens often during your own play,
it is worth an authors'-note reminder or a stronger trailing-instruction
nudge in `engine.py`'s prompt scaffolding, not a schema change.

**2. The model reliably forgets `npc.milo.pokemon_left` and milestones on a
faint, despite an explicit instruction to update both.**

> **Wrong on both halves.** The model emitted `pokemon_left` at *both* faints; the
> engine clamped it to nothing and reported it as applied. And it never emitted a
> milestone because the milestone ids were absent from the prompt entirely. Neither
> was an attention problem. See `plan/16`. `ai_instructions` in
the scenario file already says: *"decrement `npc.milo.pokemon_left` when one
of his faints"* and *"Mark milestones as they happen."* Across two separate
faints in this session (Graveler, then Onix), the model correctly reset
`active_pokemon`/`active_hp`/`active_status` for the incoming Pokémon every
time, but never once touched `pokemon_left` (stuck at `3/3` through both
faints) and never checked off "Knock out Milo's lead Graveler," even though
that milestone was unambiguously satisfied. This looks like the faint
instruction is buried inside a longer bulleted list the model is only
partially attending to. Worth trying: pull the faint-handling instructions
into their own short paragraph, or add a stat-guide line for `pokemon_left`
and the milestones that makes them as visually prominent as `hp`/`status`.

**Separately, not necessarily a bug:** `world.turn` (a `counter` stat defined
in the schema) stayed at `0` for all ten turns. `ai_instructions` never tells
the model to increment it — the instructions cover HP, status, potions,
active_pokemon, and pokemon_left, but not turn. If you want the counter to
mean something, add an explicit line telling the model to bump
`world.turn` by 1 every reply.

## Suggested next steps

1. Decide whether to patch `ai_instructions` for the two gaps above, then
   redeploy and play a few more turns to confirm faints correctly decrement
   `pokemon_left` and flip milestones.
2. Clean up the stale "Road to the Champion" scenario and adventure 42.
3. Try a status-condition move (Ivysaur's Poison Powder or similar) — this
   playtest never exercised the `status` stat changing away from `none`, so
   it is unverified in practice even though the schema supports it.
4. If DeepSeek keeps skipping state blocks more than rarely, consider the
   trailing-reminder wording in `engine.py` (`build_state_reminder` or
   equivalent) rather than switching models — the schema itself is sound.
