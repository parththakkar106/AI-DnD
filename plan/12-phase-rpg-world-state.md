# Phase 12 — RPG world state (AI-authored, engine-clamped)

**Goal:** each turn carries a structured **world state** — world stats (e.g. `day`),
player stats (`hp`, `mana`), per-NPC stats (`health`, `trust`, …) for the NPCs currently
in scene, and **milestones** (story-progress flags / quest objectives). After the player
acts, the AI reads the current state, narrates, and **proposes** state changes; the
engine **validates and clamps** them against a schema before storing. Stat meanings are
described in words (bands) so the model reasons semantically, not arithmetically.

This is deliberately the "AI owns mechanics, engine enforces limits" design — NOT a
deterministic dice engine. The AI proposes; Python is the referee.

## Design decisions (settled)

- **AI proposes, engine clamps.** The model never owns the numbers directly. It emits
  a delta; the engine applies min/max, per-turn caps, and cooldowns server-side. The
  AI cannot be trusted to obey its own frequency rules — the engine must.
- **Band descriptions are the reliability trick.** Stats carry word ranges
  (`0–20: very weak`, `20–40: hurt`, …). The model reads "he's badly hurt" and adjusts
  down, instead of doing math it's bad at.
- **NPCs = story cards.** No new NPC table. NPC stats live in the adventure's world
  state keyed by story-card id; a card is treated as an NPC when its `type` is
  character-ish (config below). Only NPCs **triggered this turn** get their stats
  injected — reuses the existing card-trigger logic in `build_context`.
- **Schema on the scenario, live values on the adventure.** The scenario is the
  template (what stats exist, their bands + rules); the adventure holds current values.
- **Milestones are sticky story flags.** Predefined objectives the AI marks reached via
  the same delta channel. Once reached they stay reached (revert only through the undo
  snapshot). Injected as "Goals" (pending) so the AI drives toward them and "Achieved"
  so it doesn't re-do them. Emergent/AI-invented milestones are out of scope for v1.
- **Flags are two-way booleans.** Separate from milestones: named on/off world/character
  state (`has_key`, `disguised`, `alarm_raised`) the AI can flip either direction via
  `"flags.<name>": true|false`. Have an `initial` value and a `desc`; no clamp/cooldown.
- **Stat guide (descriptions + band ranges).** A fixed, per-scenario legend injected each
  turn, describing each stat's `desc` and its full band ladder (`0–20 very weak, …`) —
  handled independently, so a stat may have a description, a range, both, or neither. This
  is separate from the live values block (which still shows only the *current* band label),
  giving the model the whole scale to reason across without bloating the per-turn line.
- **One-call turn.** The AI narrates AND appends a fenced state-delta block; the engine
  parses it and strips it from the visible text. No second LLM call — matters on the
  rate-limited free-tier demo (20 req/min). Parser is forgiving of messy JSON from
  weaker free models.
- **Memory bank / multi-memory is untouched.** (Confirmed.)
- **Separate from `script_state`.** World state gets its own column so it never collides
  with the scripting scoreboard, and reuses the same undo/retry snapshot pattern
  (`Action.state_before`, see `plan/11`).

---

## Data model

### Schema definition — `Scenario.stat_schema` (JSON, nullable)

Migration `(26, "ALTER TABLE scenarios ADD COLUMN stat_schema JSON")`.

```jsonc
{
  "world": {
    "day": { "type": "counter", "min": 1, "initial": 1, "desc": "In-game day",
             "max_delta_per_turn": 1, "cooldown": 0 }
  },
  "player": {
    "hp":   { "min": 0, "max": 100, "initial": 100, "max_delta_per_turn": 30,
              "cooldown": 0, "bands": [[0,20,"very weak"],[20,40,"hurt"],
              [40,60,"minor damage"],[60,90,"healthy"],[90,100,"full health"]] },
    "mana": { "min": 0, "max": 50, "initial": 20, "max_delta_per_turn": 15 }
  },
  "npc": {                                   // template applied to each NPC card
    "health": { "min": 0, "max": 100, "initial": 100, "bands": [...] },
    "trust":  { "min": -100, "max": 100, "initial": 0, "max_delta_per_turn": 20,
                "bands": [[-100,-30,"hostile"],[-30,30,"neutral"],[30,100,"ally"]] }
  },
  "flags": {                                 // two-way on/off booleans
    "has_key": { "desc": "Player holds the dungeon key", "initial": false },
    "alarm_raised": { "desc": "The enemy is alerted", "initial": false }
  },
  "milestones": {                            // sticky story-progress flags
    "rescue_gwen": { "desc": "Rescue Gwen from the bandits" },
    "reach_capital": { "desc": "Arrive at the capital city" }
  }
}
```

Per-stat rule fields (all optional, engine enforces):
- `min` / `max` — hard clamp.
- `initial` — value when first instantiated.
- `max_delta_per_turn` — largest absolute change allowed in one turn (extra is clamped).
- `cooldown` — minimum player actions between changes to this stat (0 = every turn).
- `bands` — `[lo, hi, label]` triples, used only to describe the value to the model.
- `type` — `"counter"` (monotonic, e.g. day) vs default numeric; counters reject
  negative deltas.

Milestones carry only `desc` (the objective text). They are boolean and sticky — the
engine accepts a delta of `true` only, records the action index reached, and ignores
attempts to re-set or un-set (undo is the only way back).

`npc_card_types` (scenario-level, defaults `["character","npc"]`): which story-card
types get the NPC stat template.

### Live values — `Adventure.world_state` (JSON, default `{}`)

Migration `(27, "ALTER TABLE adventures ADD COLUMN world_state JSON")`.

```jsonc
{
  "world":  { "day": 3 },
  "player": { "hp": 55, "mana": 10 },
  "npc":    { "12": { "health": 80, "trust": 20 } },   // keyed by story-card id
  "milestones": { "rescue_gwen": { "reached": true, "at": 7 } },
  "_meta":  { "last_changed": { "player.hp": 7, "npc.12.trust": 6 } }  // action index
}
```

`_meta.last_changed` backs the `cooldown` rule. NPC entries are lazily created from
the `npc` template the first time that card is triggered.

### Undo/retry snapshot — `Action.world_state_before` (JSON, nullable)

Migration `(28, "ALTER TABLE actions ADD COLUMN world_state_before JSON")`.
Snapshotted and reverted exactly like `state_before` (Phase 11) — same call sites.

---

## Turn flow

```
player input → INPUT hook (existing)
context build → inject [World State] section (current values + band scale + emit-rule)
AI response  → narration + trailing ```state { ...delta... } ``` block
             → parse delta → validate/clamp against schema → apply → strip block
             → snapshot world_state onto the action (undo)
```

### 1. Context injection (`context/builder.py`)

New always-on section `world_state`, placed with the system sections. Keep it **terse**
(competes with story history under the default context budget, raised to 16384 in Phase 12):

```
World state — day 3.
You: HP 55/100 (minor damage), Mana 10/50.
Gwen: health 80 (healthy), trust 20 (neutral).
Goals: Arrive at the capital city.
Achieved: Rescued Gwen from the bandits.
```

- Only inject NPC lines for cards **triggered this turn** — reuse `triggered` /
  `card_records` already computed in `build_context` (`builder.py:120`). No extra work.
- Milestones: list unreached ones under `Goals` and reached ones under `Achieved`
  (omit either line when empty). These are cheap and always included.
- Append the value's band label in parentheses so the model reads meaning, not just a
  number.
- Append a compact **emit rule** (once, in the narrator/system text). Instruct the model
  explicitly to:
  - end its reply with a fenced `state` block **only when something actually changed**;
  - **omit the block entirely** when nothing changed this turn (no empty `{}`);
  - include **only the stats that changed** as deltas — never restate unchanged stats,
    never send full/absolute values, e.g. `{"player.hp": -15, "npc.12.trust": +5}`.
  This keeps the emitted block tiny (saves output tokens on the free tier) and means the
  engine's clamp/cooldown logic only ever sees real changes.

### 2. Delta parse + validate (new `worldstate/` module)

New module `backend/app/worldstate/engine.py` (mirrors `scripting/` layout):

- `extract_delta(text) -> (clean_text, delta_dict)` — pull the trailing ```` ```state ````
  block, tolerate missing/extra fences, trailing commas, `+N` numbers; return `{}` on
  parse failure (never break the turn — same philosophy as a broken script).
- `apply_delta(adventure, delta, action_index) -> report` — for each `path: change`:
  1. resolve `path` (`player.hp`, `world.day`, `npc.<cardId>.trust`,
     `milestones.<id>`) against the schema; unknown paths ignored (logged).
  2. **milestone path** → accept only `true`, set `{reached: true, at: action_index}`,
     ignore if already reached; skip the numeric steps below.
  3. lazily instantiate NPC stat block from template if missing.
  4. reject if `cooldown` not elapsed (`action_index - _meta.last_changed[path] < cooldown`).
  5. clamp change to `max_delta_per_turn`; counters reject negative.
  6. apply, then clamp result to `[min, max]`.
  7. record `_meta.last_changed[path] = action_index`.
- Returns a report (applied / clamped / rejected) for the Insights panel, like the
  script report.

### 3. Wire into `generate_turn` (`routers/adventures.py:249`)

- Snapshot: `world_state_before = snapshot_world_state(adventure)` alongside the existing
  `state_before` (`:271`).
- After the `output` hook and empty-text check (`:340`): `clean, delta =
  extract_delta(text)`, `report = apply_delta(...)`, store `text = clean`, stash the
  report into `snapshot["world_state"]`.
- Persist `world_state_before` onto the AI `Action` (`:341` block) and commit
  `adventure.world_state`.
- Do this only when `scenario.stat_schema` is non-empty — zero overhead for plain
  narrative adventures.

### 4. Undo / retry (`routers/adventures.py`)

Reuse the Phase 11 wiring verbatim, in parallel:
- retry (`:449`): also restore `adventure.world_state` from the deleted AI action's
  `world_state_before`.
- undo (`:489`): also restore from the first removed action's `world_state_before`
  (fall back to `{}`).

---

## UI

- **World State panel** (play view): render current `world_state` as a readable sheet —
  world / player / per-NPC, with band label and a bar for `min..max` stats, plus a
  **milestones checklist** (reached vs pending). Reuse the collapsible-tree styling from
  the existing Play State drawer.
- **Insights**: show the parsed delta + apply/clamp/reject report per turn (next to the
  script report already there).
- **Scenario editor**: a `stat_schema` editor. **v1 is a raw JSON editor** (CodeMirror,
  reusing the script-slot editor setup) — settled, no form builder for now. A form-based
  stat builder is a possible later nicety.
- Graceful when `stat_schema` is empty: panel + editor hidden, app behaves exactly as
  today.

---

## Seed / demo

- New seeded demo scenario `seed_data/*.json` with a small `stat_schema` (player hp/mana,
  `day`, one or two NPC story cards with health/trust) so the feature is visible on the
  live demo without the player configuring anything. Keep it `:free`-model friendly.
- Extend `seed.py` idempotently (matches existing seeder contract).

---

## Exit criteria

Play the demo RPG scenario: the World State panel shows hp/mana/day, an NPC's trust, and
a milestones checklist; taking a fight action drops hp and the narration matches the new
band; a friendly action raises an NPC's trust; completing an objective marks its
milestone reached (and it stays reached); a change larger than `max_delta_per_turn` is
clamped; undo rolls every stat and milestone back to the prior turn; a plain (no-schema)
adventure is completely unaffected.

## Known limits (document, don't fix in v1)

- The AI can still narrate against the numbers occasionally; injected state + firm emit
  rule reduces but won't eliminate it. No post-narration consistency check in v1.
- No dice / skill checks / combat resolution — this phase is stat tracking only. A
  deterministic resolver is a possible Phase 13.
- NPC stats are keyed by story-card id; deleting a card orphans its `_meta`/`npc` entry
  (harmless, ignored on read).
- Cooldown/`max_delta_per_turn` are per-turn heuristics, not a full rules engine.

## Test checklist

- Schema with `max_delta_per_turn: 30`: a delta of `-50` applies as `-30`, clamps at `min`.
- `cooldown: 2` on a stat: two consecutive changes → second is rejected until 2 actions pass.
- Counter (`day`): a negative delta is rejected; `+1` advances.
- Milestone: `true` marks it reached with `at`; a second set is a no-op; `false` ignored.
- NPC stat auto-instantiates from template on first trigger at `initial`.
- Malformed / missing `state` block → turn still completes, delta `{}`, no crash.
- A turn where nothing changes emits no `state` block (and an empty `{}` is a no-op).
- Undo after a stat change restores the prior value; retry doesn't double-apply.
- Empty `stat_schema`: no World State section injected, no `world_state` writes.
