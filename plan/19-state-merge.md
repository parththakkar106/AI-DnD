# Phase 19 — One state: visible and hidden

**Goal:** the adventure carries one state object instead of two. The RPG sheet
and the script scratchpad become `visible` and `hidden` halves of it. Both the AI
and scripts can change the visible half, always through the Python referee.
Scripts keep the hidden half to themselves.

**Status: planned, not started. Last updated 2026-09-01.**

---

## Why

`Adventure.script_state` and `Adventure.world_state` were built in different
phases for different writers, and the split hardened into a rule: the AI writes
world state, scripts write script state, and neither can reach the other. The
rule was never the point. Scripts should be able to move a stat — that is what a
scripting layer is for — and the only reason they cannot is that the stat lives
in a column they were never handed.

The dividing line worth keeping is not *who writes it*. It is **whether the
value is part of the game's declared rules**:

| | visible | hidden |
|---|---|---|
| Declared in `stat_schema` | yes | no |
| Clamped by `worldstate/apply.py` | yes | no |
| Rendered into the prompt | yes | no |
| On the character sheet | yes | behind a dropdown |
| Holds arrays and deep objects | no | yes |
| Written by | AI delta, scripts, manual override | scripts |

So: merge the storage, keep the referee, and give scripts a door into the
visible half.

## What this is not

**The JavaScript `state` variable does not become the merged object.** The
engine hands a script `state` and takes back whatever `state` points to at the
end (`scripting/engine.py`, `PRELUDE` and `COLLECT`). A script that writes
`state = { turn: 1 }` replaces the whole object. Today that costs some script
variables. If `state` were the merged blob, that one line would delete every
stat, every flag and every milestone.

`state` therefore keeps pointing at the **hidden** half only. Ported AI Dungeon
scripts keep working unchanged, and no script can reach the sheet except through
the functions below.

## Shape

```python
adventure.state = {
    "visible": {...},   # exactly today's world_state, unchanged
    "hidden":  {...},   # exactly today's script_state, unchanged
}
```

`visible` is the old `world_state` dict verbatim, `_meta.last_changed` included.
That is deliberate: every function in `worldstate/` takes a plain dict
(`apply_delta`, `apply_override`, `render_state_section`, `render_reference`,
`reconcile`, `instantiate`). Passing them `state["visible"]` changes **nothing
inside those four modules**. The whole referee, renderer and schema-sync layer
moves over for free.

It also avoids a bug. `schema.reconcile` deletes any key the schema does not
define, and "Update from scenario" calls it (`routers/adventures/refresh.py:186`).
If hidden and visible were flat siblings in one dict, that button would eat every
script variable. Nested, `reconcile` never sees them.

---

## Step 1 — Merge the columns

One commit. The risky one. Nothing about behaviour changes; only where the bytes
live.

### Model

`models.py`:

- `Adventure.script_state` + `Adventure.world_state` → `Adventure.state`
  (JSON, default `{"visible": {}, "hidden": {}}`).
- `Action.state_after` + `Action.world_state_after` → `Action.state_after`
  (`CompressedJSON`, nullable, deferred).
- `Action.world_delta` is unchanged. It is the bulk-read slice for the inline
  chips and for replaying the emit block into history, and it stays its own
  column for the reason its comment already gives.

Net: two columns gone.

### NULL semantics must survive

`state_after` and `world_state_after` are nullable, and a NULL means *leave the
live state alone* — never *reset it* (see the comment on those columns, and
`attempts.restore_state`). Pre-SP4 rows carry NULLs the tree migration could not
derive.

Merged, that granularity has to be preserved per half:

- both NULL → the merged column is NULL.
- one NULL → store the merged dict with the missing half's **key omitted**.
- `restore_state` restores only the keys that are present.

Do not write `{"visible": {}, "hidden": {}}` for a row that had NULLs. An empty
dict means "this node left the state empty", which is a different claim.

### Compression

`state_after` becomes `CompressedJSON` in the same pass, like `context_snapshot`
already is (`compression.py`). The table is being rewritten anyway, and
`plan/STATUS.md` records what storage on the free tier costs when it gets away
from us.

### Migrations

Next free version is **77** (`migrations.py` tops out at 76). Follow the 43/44/45
pattern exactly — add, backfill in Python between, drop, rename — and register a
named version constant plus a hook in `bootstrap`.

```
77  ALTER TABLE adventures ADD COLUMN state JSON
    -> _backfill_adventure_state(conn)      # fold script_state + world_state
78  ALTER TABLE adventures DROP COLUMN script_state
79  ALTER TABLE adventures DROP COLUMN world_state

80  ALTER TABLE actions ADD COLUMN state_after_z BLOB / BYTEA
    -> _backfill_state_merge(conn)          # fold + compress, round-trip verified
81  ALTER TABLE actions DROP COLUMN state_after
82  ALTER TABLE actions DROP COLUMN world_state_after
83  ALTER TABLE actions RENAME COLUMN state_after_z TO state_after
```

`_backfill_state_merge` destroys data if it is wrong, so it copies
`_backfill_context_snapshot`'s discipline: batch in `SNAPSHOT_BATCH` rows,
`compression.unpack(compression.pack(v)) == v` on every row, and raise rather
than skip on a mismatch. The loop is one transaction, so a raise rolls the DROPs
back and the originals are still there.

Read the JSON defensively — SQLite hands back a string, psycopg hands back a
parsed value. Both backfills already do this.

### Call sites

Backend, all mechanical:

| file | what |
|---|---|
| `models.py` | the two columns above |
| `attempts.py` | `restore_state`, `snapshot_outcome` — one half each becomes one object; keep the per-key NULL rule |
| `tree.py:stamp_outcome` | same |
| `scripting/pipeline.py` | reads/writes `state["hidden"]` |
| `context/builder.py:_script_memory` | `state["hidden"]["memory"]` |
| `routers/adventures/turns.py` | `apply_delta` against `state["visible"]` |
| `routers/adventures/crud.py` | adventure creation; the two GET endpoints; the override PUT |
| `routers/adventures/refresh.py` | `reconcile` against `state["visible"]` |
| `routers/adventures/nodes.py`, `takes.py` | `undefer(state_after)`, drop the second undefer |
| `bundle.py` | see below |
| `tools/` | `branch_fixture`, `tree_fixture`, `stress_session`, `memory_ab`, `measure_bundle` |

`schemas.py:193` has a field also called `world_state` — it is the refresh
report's added/removed **paths**, not state. Rename it `stats` while here, or
leave it; it is unrelated.

### API

Collapse `GET /adventures/{id}/script-state` and `GET /adventures/{id}/world-state`
into:

```
GET /adventures/{id}/state   -> { visible, hidden, schema }
PUT /adventures/{id}/state   <- { visible: { "player.hp": 40, ... } }
```

`schema` stays null when the adventure has no RPG layer. The PUT keeps today's
behaviour: 400 without a schema, and each path validated one at a time through
`apply_override`.

### Bundles

`bundle.py` writes `scriptState` / `worldState` at the top and `stateAfter` /
`worldStateAfter` per node. Export becomes `state` and `stateAfter`.

Keep `FORMAT = "ai-dnd-adventure-v2"`. Do not bump. The reader already tolerates
two shapes, so teach it a third: when a bundle carries `scriptState` or
`worldState`, fold them; when it carries `state`, take it as-is. Same for the
node keys. Files already exported to somebody's disk keep importing.

### Tests

22 of the 44 backend test files touch these columns. Nearly all of them just
build `models.Adventure(..., script_state={}, world_state={...})`; those become
`state={"visible": {...}, "hidden": {}}`. Worth a small fixture helper so the
next change to this shape is one edit.

Give the merge its own test: an adventure at each of the four NULL combinations
round-trips through the migration with the same `restore_state` behaviour it had
before.

---

## Step 2 — Commit once per node, not once per script

One commit. Small, and it fixes a bug that already exists.

`ScriptPipeline.run` calls `self.db.commit()` after **every script**
(`scripting/pipeline.py:105`). That commit is not paired with an action row and
not paired with a snapshot. If the request dies between it and the turn's own
commit, the state is durable but no node carries it, so undo cannot reach it. On
the retry path the `finally` in `turns.py:145` repairs this; on a fresh turn
nothing does.

Today that only strands script variables. After step 1 it strands the character
sheet.

**Fix: replace `self.db.commit()` in the loop with `self.db.flush()`.**

The turn's own commits are already correct and stay as they are — the player
action commits with its `snapshot_outcome` at `turns.py:382`, the AI action at
`turns.py:312`. Both pair a node with the state it left behind, which is exactly
the invariant.

Checked before proposing this:

- `SessionLocal` is `expire_on_commit=False, autoflush=False`
  (`database.py:62`), so the current commit is not what makes anything visible
  to the next script in the chain. Card visibility does not change.
- All three production `ScriptPipeline(...)` sites (`turns.py:356`,
  `takes.py:66`, `takes.py:296`) run inside a turn that commits afterwards.
- A failed turn now rolls the session back and leaves the state untouched,
  which is what it should always have done.

---

## Step 3 — Let scripts move the visible half

One commit. This is the feature the whole phase is for.

### The JS surface

```js
state                        // hidden half — free-for-all, exactly as today
getStat(path)                // current value, or undefined for an unknown path
setStat(path, value)         // absolute
adjustStat(path, delta)      // relative
```

Paths are the ones the schema already defines and the AI already uses:
`player.hp`, `world.day`, `npc.gwen.trust`, `flags.raining`,
`milestones.escaped`.

### How it works

`__DATA__` gains two flat maps, built in Python from the schema and
`state["visible"]`:

```
stats  = { "player.hp": 40, "npc.gwen.trust": 10, ... }
bounds = { "player.hp": {"min": 0, "max": 100, "type": "number"}, ... }
```

Flat by path, because that is what the API addresses. The prelude keeps a local
copy, applies min/max locally on write, and records the write:

```js
var __writes = [];
function getStat(path) { return __stats[path]; }
function setStat(path, value)   { /* clamp via __bounds, update __stats, push */ }
function adjustStat(path, delta) { setStat(path, (__stats[path] || 0) + delta); }
```

Clamping locally is what makes `getStat` honest: a script that writes then reads
sees the value Python will actually store, not an unclamped guess. `COLLECT`
returns `__writes` alongside `state`.

Python is still the authority. After each hook returns, `pipeline.run` applies
the writes through **`worldstate.apply_override`** — which already exists, is
already the manual-author-edit path, and is already the right semantics:

- validates the path and the type, rejects unknown ones,
- clamps to min and max,
- **ignores `cooldown` and `max_delta_per_turn`**, because those exist to check
  an AI guess, not a rule the author wrote deliberately,
- does not stamp `_meta.last_changed`, so a script write does not spend the AI's
  cooldown budget.

An unknown path is rejected there and surfaces in the existing script error
list, next to `log()` output.

Applying per hook rather than at the end of the turn means the next script in
the chain sees the updated value.

### Ordering within a turn

Leave it as it is and write it down: `onOutput` scripts run at `turns.py`
before the AI's delta is extracted and applied, so a script's write lands first
and the AI's delta applies on top of it.

That is the wrong order for a script trying to enforce a floor after the AI
moved a value. Not fixing it here — flipping it means moving the world-state
block ahead of the output hook, which changes what `text` the hook sees. Open
question, own change.

### Chips

`Action.world_delta` feeds two different things: the inline chips under a
message, and `context/builder.py:_history_text`, which replays the emit block
into the prompt so the model keeps producing one.

**Script writes must not go into `delta`.** Replaying them would teach the model
to emit changes it never made. Add a separate `script_applied` list to
`world_delta` for the chips only, and leave `delta` meaning "what the AI
emitted".

Optional, and it can land after the rest of step 3 works.

---

## Step 4 — One drawer

One commit. No backend risk.

Fold `StatusDrawer` into `WorldStateDrawer`: the character sheet on top, and the
hidden half under a collapsed "Script state" dropdown, rendered by the existing
`StateTree` / `StateValue` components, which already handle arbitrary JSON.

`api.getScriptState` and `api.getWorldState` become one `api.getState`.

---

## Order and risk

| step | risk | independent? |
|---|---|---|
| 1 — merge columns | high (live data, table rewrite) | — |
| 2 — commit per node | low, fixes an existing bug | yes, could ship first |
| 3 — script stat API | medium | needs 1 |
| 4 — one drawer | none | needs 1 |

Ship 1 and 2 on their own and let them sit through some real play before
starting 3. Step 2 is genuinely independent of the merge and could go first if
we want a warm-up.

## Before touching production

`git pull --ff-only` first — the daily Action force-commits, and Render's
database is the one from `plan/STATUS.md` that needed a `VACUUM FULL` to give
back 79 MB. Expect to run one after migrations 80–83 rewrite `actions`, and
read the sizes from the column sums rather than `n_live_tup`, for the reason
recorded there.
