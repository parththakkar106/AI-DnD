# Phase 14 — Story tree (branching adventures)

**Goal:** replace the linear action list with a **tree**. A retry becomes a sibling
rather than a rewrite; continuing from one makes it a branch. Players can go back to any
turn, take a different path, and keep both — switching between them freely.

Depends on `13-memory-embedding-cost.md` shipping first: memory retrieval is the one read
a tree cannot window, so it is the cost floor of a turn under this design. Fix the floor
before building on it.

## Why (beyond the feature)

Seven bug classes stop existing, and every one traces to the same root — **the story is a
mutable list**:

| Bug | Why it goes away |
|---|---|
| Deleting a middle action skips a later one forever (`note_action_removed`) | Cursors become node ids, not positions in a shifting list |
| `prune_dangling_memories` orphaning actions behind the cursor | Nothing is ever removed |
| Editing a summarised action leaves its memory stale forever — **currently unfixed** | Editing makes a new node; the old memory stays correct for the old path |
| The one-turn memory holdback (`settled_story_actions`) | Nothing is mutated, so nothing goes stale — the concept is unnecessary |
| The legacy-cursor no-rewind trap found while fixing that | Same |
| "Anything reading `adventure.actions` during generation must exclude the retried action" — leaked into 4 call sites | The replaced turn is not on your path; it cannot leak |
| `variant_count` / mirrored `text` drifting from `variants` | The denormalisation disappears entirely |

It also resolves the 1NF violation: `variants` as a JSON repeating group becomes rows.

## Design decisions (settled 2026-08-16)

- **Full branching, with UI.** Not the "tree schema, no branch picker" middle option —
  branching ships as a feature people use.
- **`branch_id` + `depth` on every node, not parent pointers alone.** Parent pointers
  alone mean walking N links to read a story, which throws away the round-two windowing
  work. `depth` replaces `index` as the ordering key.
- **A `branches` table with `parent_branch_id` and `fork_depth`.** The fork point is
  **stored at fork time, never inferred**. Nothing is copied on a fork — a branch borrows
  its ancestors' turns.
- **Lineage cached on the branch row.** `lineage = [(D,∞), (C,45), (B,30), (A,10)]`,
  computed once at fork (parent's lineage + one entry). Reads never walk to reconstruct
  it. Each ancestor is capped at the `fork_depth` of the branch beneath it.
- **Read the lineage lazily, windowed.** Query the newest few lineage entries, measure,
  fetch more only if the context budget is not covered — the exact shape of
  `history.window_covering()`. **Clause count is bounded by the context window, not by
  fork count**, so a 200-fork story reads as cheaply as a 2-fork one.
- **Promote to a branch only on continue.** Attempts at the tip stay as sibling leaves;
  one becomes a branch the moment a turn is played past it. Keeps the lineage chain to
  "divergences I built a story on", not "every retry ever" — the difference between a
  handful of entries and fifty.
- **Memories and the summary attach to the node that produced them**, found by walking
  up. Shared ancestors are shared automatically, so a fork costs nothing and **nothing
  needs recreating**. A memory covering depths 37–42 hangs off that branch's node 42 and
  is invisible to any path not through it. Generalise the rule: *anything derived attaches
  to the node that produced it* — the summary included, so stop storing it per turn.
- **Full lineage for memories, windowed lineage for the story.** Memory retrieval is
  long-range recall and cannot be windowed, but memories are sparse (~1 per 6 actions), so
  a long OR-clause returning ~33 small rows is fine. Two queries, one lineage.
- **Never auto-prune.** Nothing is deleted without an explicit user action. **This makes
  a branch-management UI a hard dependency, not a nice-to-have** — storage grows without
  limit otherwise.
- **Story cards stay adventure-wide.** A card invented on branch B shows on branch A.
  Already true for undo today (script card mutations are not reverted), so this is a
  documented limit, not a regression. Explicitly rejected event-sourcing card changes onto
  nodes.
- **State carries over almost free.** `state_before` / `world_state_before` already
  snapshot the script scoreboard and RPG world state per action, and `apply_variant()`
  already restores them on a switch — a branch switch is the same move. Wrinkle: those are
  *before* pictures; a node wants the *after*. And they are NULL on pre-column rows.

## Schema sketch

```
branches(id, adventure_id, parent_branch_id, fork_depth, lineage JSON, created_at)
actions(id, adventure_id, branch_id, depth, type, text, reasoning,
        world_delta, state_after, world_state_after, context_snapshot, created_at)
memories(..., branch_id, depth)          -- attached to the node that produced it
adventures(..., head_branch_id, head_depth)
```

Reading branch C, tip at depth 7, lineage `[(C,7), (B,5), (A,3)]`:

```sql
SELECT * FROM actions
WHERE (branch_id='C' AND depth <= 7)
   OR (branch_id='B' AND depth <= 5)
   OR (branch_id='A' AND depth <= 3)
ORDER BY depth DESC LIMIT 32
```

→ `A0 A1 A2 A3 B4 B5 C6 C7`. Depth is a position along *a* path, not a global turn
number — `A4` and `B4` are alternatives, not duplicates.

## Work

1. `branches` table, `branch_id`/`depth` on `actions`, `head_*` on `adventures`.
2. Lineage computation + a **single module that owns the branch clause** — same role
   `context/history.py` plays today. Every query must go through it; one forgotten clause
   shows the wrong story, quietly.
3. `history.py` rewritten against lineage windowing. `window_covering` keeps its shape.
4. Memories/summary attached to nodes; delete the cursor-position machinery
   (`position_of_index`, `settled_*`, `note_action_removed`, `_rewind_cursors_to_index`).
5. Sibling storage for un-promoted tip attempts + the promotion step.
6. Node state moves from *before* to *after* snapshots.
7. Migration: every existing adventure becomes branch A; `index` → `depth`; `variants`
   entries become sibling nodes; `variant_index` becomes the head pointer.
8. Frontend: branch picker replacing `VariantPager`, plus branch management (rename,
   delete, switch) — required, given no auto-pruning.

## Open

- **`retry_of.index` reuse** stops the world-state cooldown clock advancing on a re-run of
  the same turn. Whatever replaces it must preserve that. (Carried into SP5 below.)

Everything else that stood open here was decided on 2026-08-17 — see the next section.

---

# Implementation plan (decided 2026-08-17)

Four decisions, taken before any code was written:

| Question | Decision |
|---|---|
| Phased or single migration? | **Structural-first, no runtime flag.** SP1–SP4 migrate to the tree *representation* with behaviour identical to today. Branching features layer on after. |
| How destructive is the migration? | **Keep the legacy columns for one release.** `index`, `variants`, `variant_index`, `variant_count` stay, unread, until the tree is proven live (SP8 drops them). |
| Branch-management UI scope | **Full tree visualisation** — a spatial view of forks, not just a picker. |
| Export bundle | **`ai-dnd-adventure-v2`**, with a v1 legacy reader so existing bundles keep importing. |

**Why no feature flag.** A linear story *is* a tree with one branch, so the intermediate
states are not half-migrated — they are the same product with a superset schema
underneath. That makes "current adventures are unaffected" a literal, testable pass
condition for every subphase up to SP4, which a flag would have replaced with two live
code paths through the context builder, the memory bank, undo and retry at once.

## The regression contract

`tests/test_story_tree_baseline.py` (built in SP0) drives the whole product over HTTP —
create, play, retry, switch, undo, page with `before_id`, memories, summary, export — and
asserts only on API responses, never internals.

**It must pass unmodified through SP1, SP2 and SP3.** That is the contract those
subphases are verified against. SP4 is the first subphase permitted to change it, and
even there only where the change is deliberate and named below.

## Traps found while reading (these are the ones that bite quietly)

- **`history._from_memory()` slices `adventure.actions`.** The "never load twice"
  shortcut returns whatever is already in the session — which under a tree is *every
  branch's* actions, not the path. It would silently assemble context from siblings.
  This is the single highest-risk line in the change; SP2 must make the shortcut
  branch-aware or delete it.
- **`scripting/pipeline.py::_history()` and `_info()` read `adventure.actions` directly**,
  and hand it to user scripts as the documented history API. Same trap, user-visible.
- **`Adventure.actions` is `order_by="Action.index"`.** Ordering it by `depth` is not
  enough — the collection is still every branch. Anything iterating it needs the path.
- **`limits.check_row_cap("actions")` counts every action in the adventure.** With no
  auto-pruning, a branched adventure hits `MAX_ACTIONS_PER_ADVENTURE` while its *story*
  is far shorter. The cap has to count the tree but be explained as the tree, or move.
- **The holdback cannot die in SP3.** `settled_story_actions` exists because retry
  mutates a row in place. Retry stops mutating in SP4, so the holdback is only safe to
  delete there — deleting it in SP3 reopens the exact bug it was written for.

## Subphases

Each ships independently, on its own branch, green before the next starts.

### SP0 — Baseline and regression net *(no product change)*

- `tests/test_story_tree_baseline.py` — the contract above. 24 tests covering opening a
  windowed adventure, the turn engine (do/say/story/continue), script effects, retry and
  variant switching, undo, paging up to the start, edit/delete, memories, export/import
  round-trip, and world state.

**Done, 2026-08-17.** 259 existing tests green (17.8s), then **283 green** with the
baseline added, against unmodified `main`. That is the number every subphase below is
measured against.

A pre-migration (**schema 45**) fixture is needed too, built by a script rather than
committed as a binary — but its only consumer is the SP1 migration test, so it lands
there.

#### `--rich`: a correctness fixture beside the scale one

The measuring fixture is sized from production and leaves every column it does not weigh
at its default. Checked against a freshly built one, that is exactly the set of columns a
tree has to migrate: `state_before`/`world_state_before` NULL on all 600 rows, no
scenario and so no RPG layer, no adventure scripts, both cursors 0, and 100 retry
histories whose two attempts carry **byte-identical text with `variant_index` always 0** —
so "which attempt is live?", the one question SP4's migration answers, had no observable
answer.

`tools.stress_session --rich` fills in precisely those and nothing else:

```
cd backend
.venv/Scripts/python.exe -m tools.stress_session --rich --actions 30 --memories 12
```

An RPG scenario built from the real seed schema with a world state played forward (hp
100 → 71, flags flipping partway); per-action `state_before`/`world_state_before`
snapshots that are monotonic, so a bad rollback reads as a wrong number rather than as
nothing; a gold script on the adventure; story cards; non-zero memory and summary
cursors with a real `story_summary`; pinned and forgotten memories; retry attempts with
distinct texts, counts of 2 *and* 3, and a live attempt that is **often not the last one
written**; and a second adventure, so "does this leak across adventures?" is answerable —
a branch clause that forgot its adventure would still look correct on a database holding
exactly one.

It is a correctness fixture, so prefer it small: what it is for is variety per row, not
rows. **Its byte figures are not comparable to a plain run** and it does not replace the
scale fixture — the plain one still holds the egress ceilings, and was re-measured
unchanged (1.8 kB, actions 1.7 kB) after `--rich` was added.

The invariant `text == variants[variant_index]["text"]` holds on every retried row, and
is asserted when the fixture is built. SP4's migration reads exactly that to decide which
sibling becomes the head.

### SP1 — Schema and migration *(no behaviour change)*

| File | Change |
|---|---|
| `app/models.py` | New `Branch` model. `Action.branch_id`, `Action.depth`. `Adventure.head_branch_id`, `head_depth`. `Memory.branch_id`, `Memory.depth`. Legacy columns untouched. |
| `app/migrations.py` | Migrations 46+: create `branches`; add columns; index `(branch_id, depth)`; backfill. Dialect map wherever BLOB/BYTEA-style spellings diverge. |

Backfill: one branch per adventure, `lineage = [(A, ∞)]`; `actions.branch_id = A`,
`depth = index`; `adventures.head_*` from `max(index)`; memories take branch A and a
depth derived from `source_end`.

**Verify:** baseline test unmodified. New `tests/test_tree_migration.py` — every action
carries a branch and `depth == old index`, no row lost, head pointers correct, memories
mapped. Bootstrap run twice is a no-op. `tests/test_egress.py` ceilings unmoved (two
integers a row). **Post-deploy `VACUUM FULL actions;` is mandatory** — this rewrites
every row, which is the 144 MB lesson at the top of `STATUS.md`.

**Done, 2026-08-17** (branch `sp1-tree-schema`). **297 tests green**, the 283 from SP0
plus 14 in `test_tree_migration.py`. The baseline contract passes **unmodified**, which
was the pass condition. Five things the plan did not anticipate, all of them found by
building it:

- **The writes could not wait for SP2.** The file table above lists only `models.py` and
  `migrations.py`, but a migration never visits a row written *after* it runs — so
  shipping the columns without a writer would leave every turn played between the two
  deploys with no branch, and from SP2 on a row with no branch is a row no read can see.
  `app/tree.py` is that writer: `root_branch` / `head_branch` (get-or-create),
  `place_action`, `place_memory`, `refresh_head`. One module for the same reason SP2 gets
  one — a node written without a branch fails by *disappearing*, not by raising. Wired
  into create/turn/import/undo/delete/memory, plus `seed_demo.py` and
  `tools.stress_session` (a fixture built by `create_all` is stamped LATEST, so no
  migration ever runs against it).
- **`adventures.head_branch_id` cannot be a foreign key.** `branches.adventure_id`
  already points the other way, and the pair is then a cycle `create_all` refuses to
  order; the fix for that is `use_alter`, which SQLite has no ALTER for. It is a plain
  integer, documented as a cache, and `head_branch` recovers onto the root if it ever
  names a branch that is gone.
- **`lineage` is NOT NULL, because `branches` comes from `create_all`.** The backfill
  cannot insert a row and fill the lineage afterwards via a NULL marker, so it inserts
  `'[]'` and guards step two on `json_array_length(lineage) = 0` — not `= '[]'`, because
  Postgres `json` has no equality operator.
- **SQLite cannot drop a column a foreign key names.** So a current-schema database
  cannot be rewound past `branch_id` at all, which broke the two existing tests that
  simulate an old database by rewinding only the *stamp*. Fixed properly:
  `migrations._column_already_there` makes every `ADD COLUMN` idempotent (the
  `IF NOT EXISTS` the module docstring asks for and SQLite has no syntax for), and
  `tests/schema_rewind.py` holds the inverse of the migrations that *can* be undone.
  The SP1 fixture therefore builds a **genuine schema 45** by dropping the three tables
  and recreating them from frozen pre-tree DDL, so the real ALTERs run — including the
  one that adds a foreign key.
- **Deleting a branch takes its nodes with it**, and deleting an adventure takes its
  branch — both verified, both at the database level via `ON DELETE CASCADE` on the two
  `branch_id` columns. SP7's delete-a-branch needs no code of its own for the nodes.

Measured: `branches` costs **0.1 kB of a 733.5 kB turn (0 %)**, and the page load
(62.6 kB) and index (1.8 kB) shapes are byte-identical to the figures in `STATUS.md`.
One cost that is *not* free: the new table and its index add ~47 ms to every
`create_all`/`drop_all` cycle on SQLite, and the suite does one per test — 20 s → 38 s.
Test-only (DDL fsync), so no model change; if it ever matters, the fix is the test
harness, not the schema.

### SP2 — The branch clause *(reads move to lineage; still one branch)*

One module owns the clause; every action read goes through it. A forgotten clause shows
the wrong story, quietly, which is why it is one module and not a convention.

| File | Change |
|---|---|
| `app/context/lineage.py` *(new)* | Lineage computation and the branch clause. The only place that knows how a path is selected. |
| `app/context/history.py` | `_filters` takes the clause; order by `depth`; `window_covering` keeps its shape. **Fix `_from_memory`.** |
| `app/routers/adventures.py` | `action_window` anchors on the anchor's `depth`; `last_action`, `next_index`→`next_depth`, `_latest_narration`. |
| `app/scripting/pipeline.py` | `_history()`/`_info()` read the path, not the collection. |

**Verify:** baseline test unmodified. `test_history_window.py`, `test_action_paging.py`
green. New test builds a **two-branch fixture directly in the DB** and asserts the design
doc's own example reads back as `A0 A1 A2 A3 B4 B5 C6 C7`, and that a sibling's nodes are
invisible. Egress: a 20-fork fixture costs within a small factor of a 1-fork one —
clause count is bounded by the context window, not by fork count.

### SP3 — Memories and summary attach to nodes

Cursors stop being positions in a shifting list. `memory_cursor`/`summary_cursor` become
node-anchored `(branch_id, depth)`.

Deleted: `position_of_index`, `note_action_removed`, `_rewind_cursors_to_index`,
`prune_dangling_memories`, and their call sites in `undo_turn` and `delete_action`.
**Kept until SP4:** `settled_story_actions` and the holdback (see traps).

**Verify:** baseline test unmodified. `test_memory_settling.py` and
`test_memory_retrieval.py` updated, plus a new branch-isolation test — a memory created
on branch B is invisible from branch A, and shared ancestors are visible from both.
Memory retrieval reads the *full* lineage (it cannot be windowed) but stays sparse:
assert the byte cost on a deep fork.

### SP4 — Variants become sibling nodes

Retry stops mutating a row. It writes a sibling leaf at the same depth.

Deleted: `set_variants`, `variant_of`, `apply_variant`, `VARIANT_SNAPSHOT_KEYS`, and the
holdback. Node state moves from *before* to *after* snapshots (`state_after`,
`world_state_after`) — a sibling needs its own outcome, so this belongs here rather than
in its own subphase. Pre-column rows are NULL and must stay tolerated.

A migration converts existing `variants` JSON into sibling rows — reading the legacy
column that decision 2 kept, which is the whole reason it was kept.

`/variants` and `/variant` keep their URLs and response shapes here, re-implemented over
sibling rows, so the frontend keeps working until SP7 replaces it.

**Verify:** the first subphase allowed to move the baseline test, and only for
`variant_count`/`variant_index` semantics — the retry *outcomes* must not move.
`test_retry_variants.py` rewritten against siblings but asserting the same observable
results, including that the gold script is not double-applied. `test_state_revert.py`
against after-snapshots. Migration test: attempt count preserved, the active attempt
becomes the head.

### SP5 — Fork on continue

Playing past a non-head sibling promotes it: a `branches` row with `parent_branch_id`,
`fork_depth`, and a `lineage` computed once from the parent's. Nothing is copied.
`adventures.head_*` moves. **The cooldown clock must not advance on a re-run of the same
turn** — the one item still open above.

**Verify:** a fork creates exactly one branch row and copies no actions; lineage is
correct and capped at each `fork_depth`; both branches read independently; switching
restores the right script/world state; the cooldown test from `test_worldstate.py`
still holds across a retry.

### SP6 — Export/import v2

`ai-dnd-adventure-v2` carries branches and nodes; import accepts v1 and v2, mapping a v1
bundle's linear actions plus `variants` onto one branch with siblings.
`limits.check_bundle_lists` learns about branches.

**Verify:** v2 round-trip of a branched adventure is lossless; a v1 bundle still imports;
a bundle claiming more branches than rows is rejected rather than half-applied.

### SP7 — Frontend: full tree visualisation

`VariantPager` is removed. A spatial tree view replaces it, plus switch, rename and
delete-with-confirm. `api.js` gains the branch endpoints.

**Verify:** this is where the standing open gap gets closed — **drive the 600-action
`--keep` fixture in a browser by hand**, on the same scroll path that has never been
driven and already hid one bug. A vitest + jsdom harness covers the prepend arithmetic;
jsdom has no layout, so scroll position still needs eyes.

### SP8 — Drop the legacy columns

Only once the tree is proven live. Migration drops `index`, `variants`, `variant_index`,
`variant_count`, followed by `VACUUM FULL actions;`.

**Verify:** full suite; egress ceilings; a measured before/after size, aggregates only.

## Standing constraints

- **No production data is read at any point in this phase.** Migrations, the e2e
  baseline and every egress measurement run on local SQLite and the synthetic `--keep`
  fixture. If a real Postgres is ever needed for a write path, it is a throwaway
  database whose name contains `stress`/`scratch`, and it is asked for first.
- **Any migration that rewrites `actions` is followed by `VACUUM FULL actions;`** on the
  direct endpoint, not `-pooler`. SP1, SP4 and SP8 each rewrite every row.
