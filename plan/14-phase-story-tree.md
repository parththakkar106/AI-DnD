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

**Scored, 2026-08-18, with SP1–SP5 shipped.** Six of the seven are gone as described.
The seventh — the one-turn holdback — is gone too, but the reasoning above was wrong
about *why* it could go: siblings share a coordinate, so replacing what a turn says still
invalidates the memory covering it. What made it deletable is that the repair already
existed for undo and delete; see the trap note below. Two rows want a footnote:

- **Editing a summarised action** is still unfixed. The table says editing makes a new
  node; nothing in SP1–SP5 makes it do that, and no subphase is scheduled to. `PATCH
  /actions/{id}` still writes over the text in place, and the memory covering it goes
  stale exactly as before. What *has* changed is that the machinery to fix it now exists
  — an edit could write a sibling and switch to it, which is a retry the player typed —
  so it is a small change whenever it is wanted.
- **The 1NF violation** is resolved in the database. The `variants` array survives in
  exactly one place: the v1 export bundle, which SP6 replaces.

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

Nothing. The last item — **`retry_of.index` reuse**, which stopped the world-state
cooldown clock advancing on a re-run of the same turn — was closed in SP4 by reusing the
retried node's *depth*, and pinned by a test in SP5. Everything else that stood open here
was decided on 2026-08-17; see the next section.

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
  delete there — deleting it in SP3 reopens the exact bug it was written for. It survived
  SP3 as `memorybank.settled_after`, which is the `- 1` in "how much story is past the
  mark"; that subtraction is the whole of it.

  *Closed in SP4, but not for the stated reason.* Siblings share a coordinate and the
  mark names the coordinate, so replacing what a turn says still invalidates the memory
  covering it — retry mutating a row was never the whole of the problem. What let the
  holdback go is that the right repair (`forget_node` plus a rewind) already existed for
  undo and delete, and retry and a sibling switch now make it too. **If a mark still
  needs correcting when the story changes, correct it; do not decline to make the mark.**

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

**Done, 2026-08-17** (branch `sp2-branch-clause`). **317 tests green**, the 297 from SP1
plus 20 in `test_branch_clause.py`. The baseline contract passes **unmodified**, which
was the pass condition. Six things worth not rediscovering:

- **The contract forced the write side, not the read side.** The SP0 baseline writes its
  actions straight to the database and must pass unmodified — so "every writer calls
  `place_action`" could not be the invariant, because the baseline is a writer and does
  not. Neither did any of the eleven other test fixtures. The alternative was a read
  tolerant of a NULL branch, which is the quiet-wrong-story failure this subphase exists
  to make impossible. So the session enforces it instead: `tree.place_new_nodes` runs
  from `Session.before_flush` and places anything unplaced, registered in `models.py` so
  that importing the models arms it. The call sites keep their explicit calls — a node
  placed at the call site is placed *before* the code around it reads the row back.
- **A branch could no longer be created with a flush.** `root_branch` did
  `db.add(); db.flush()` to get the id its lineage names, and a nested flush inside
  `before_flush` raises. It inserts through Core and reads the row back — same
  transaction, three statements, once per adventure ever.
- **The identity map holds weak references, and it cost 25 % of the suite.** Resolving
  the head branch per node re-read the `branches` row for every node in the flush, because
  nothing held a strong reference between two calls: **201 SELECTs to write 200 actions**,
  and 36 s → 45 s on the same 297 tests measured back to back. The head is now resolved
  once per adventure per flush — **2 SELECTs**, and the same 297 tests then time within
  noise of SP1 (44.2 s each; this machine's load drifts by ~20 % between runs, so trust
  the statement count, not the stopwatch). Pinned by a test that counts reads of
  `branches`, because the stopwatch is all the symptom there ever was.
- **The index screen is the one read scoped by head branch rather than by lineage.**
  `_latest_narration` picks one row per adventure for a hundred adventures at once, and a
  lineage clause each would put hundreds of OR-terms on that query. The two answers differ
  only for a branch with no nodes of its own, which cannot exist — a branch is created by
  playing a turn onto it.
- **Two reads are deliberately left un-pathed**, both documented where they live.
  `max_action_index` allocates the legacy `index`, which must stay adventure-wide or two
  branches issue the same number; and export is a flat v1 bundle whose reader has no idea
  branches exist, which is why SP6 replaces the format rather than widening the query.
  A third is a known divergence, not a decision: the index screen's `action_count` counts
  the tree, and will overstate a branched story until SP5.
- **`Adventure.actions` was left ordered by `index` on purpose.** Ordering the collection
  by depth would not make it a story — it is every branch's actions, and a path is a
  selection out of it. What the relationship is still for is ownership and the
  delete-orphan cascade.

Measured: a story forked **20 times reads its newest 32-action window for 3,178 B against
the 2,961 B an unforked story of the same length costs (1.07×)**, naming one branch of its
22 lineage entries. The pre-tree 600-action `--keep` fixture was migrated and then driven
over HTTP end to end: index **1,840 B**, page load **64,149 B** — the same shapes as
before the phase — and scrolling to the start took 9 pages and saw all 600 actions exactly
once.

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

**Done, 2026-08-18** (branch `sp3-node-cursors`). **330 tests green**, the 318 the branch
started from plus 12 — 11 in `test_branch_clause`'s new sibling `test_memory_nodes.py`
and one on migration 56. The baseline contract passes **unmodified**, which was the pass
condition. `app/context/cursors.py` is the new module; migrations 53–56 add
`memory_cursor_branch_id/_depth` and `summary_cursor_branch_id/_depth` and translate the
old counts into them.

Seven things worth not rediscovering:

- **An anchor is a coordinate, not a pointer, and that is what deleted the machinery.**
  Half of this subphase was expected to be rewriting the cursor bookkeeping in depth
  terms. None of it needed rewriting: `count_after(41)` is well defined with node 41
  deleted, and deleting node 12 does not change what "past node 41" means. So
  `note_action_removed`, `_rewind_cursors_to_index`, `position_of_index` and the
  post-turn clamp did not become depth-shaped versions of themselves — they became
  nothing. **If a mark still needs correcting when the story changes, it is still a
  position.**
- **The clamp had its own trap and it also goes.** `run_post_turn` clamped both cursors
  to the story length every pass, deliberately against the *full* count, because
  clamping to the settled count rewound a caught-up adventure a step and re-covered an
  action. An anchor past the tip is not a broken value: `settled_after` reports nothing
  to do, and the story growing back past it resumes exactly where it left off.
- **`prune_dangling_memories` became a lookup, and got stricter by accident.** A memory
  hangs off the node its block ends on, so "what did this node produce?" is
  `(branch_id, depth)` — `memorybank.forget_node`. The scan it replaces could only ever
  notice damage *after* the fact (a covered range past `max(index)`), and could not
  notice at all when the node was deleted from the middle of a story that still had
  later actions. Withdrawing the memory is half the job: the ground it covered is still
  behind the mark, so the mark goes back to `source_start - 1` — a depth, whether or not
  a row still sits there.
- **A memory with no node had to be spelled out in the clause.** A hand-written memory
  summarises nothing, so it carries a branch and a NULL depth. Every ancestor entry in a
  lineage clause is capped `depth <= fork`, and NULL fails that — so a typed memory would
  have become invisible at the first fork after it was written, with nothing to see but a
  prompt that stopped mentioning it. `Path.clause(unanchored=True)` is that case, and
  actions never pass it: an action with no depth is a pre-tree row no read should see.
- **Retrieval reads the whole lineage, and it is free.** Measured on two stories of 84
  actions and 14 memories each, one flat and one forked twenty times: **1,807 B against
  1,823 B**. The clause carries 22 branch terms instead of one, and the clause is not what
  crosses the wire. The egress shapes are otherwise byte-identical to SP1's — index
  1.8 kB, page load 62.7 kB, turn 733.8 kB.
- **Three reads stay adventure-wide, deliberately.** Embedding and eviction are facts
  about the row and about the bank, not about the path — skipping a sibling's memories
  would only mean embedding them at the moment somebody switched to them, and evicting the
  memories of a story nobody is reading is the right thing to evict first. The Memories
  drawer is management rather than retrieval, and hiding a branch's memories there would
  make them unfindable in a phase whose rule is that nothing is removed automatically.
- **The v1 bundle still speaks positions, in exactly two places.** Export counts the
  anchor back into a position; import translates the other way, but only after the
  actions exist, because that is the one moment the two coordinate systems can be lined
  up. `cursors.position_of` and `cursors.anchor_at_position` are the whole of what still
  knows about positions, and SP6's v2 format retires them.

Migrations 53–56 rewrite `adventures`, not `actions` — a few hundred rows against a few
hundred thousand — so **this deploy needs no `VACUUM FULL` of its own**. The one SP1 owes
is still owed.

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

**Done, 2026-08-18** (branch `sp4-sibling-nodes`). **347 tests green**, the 330 SP3
finished with plus 13 in the new `test_attempt_siblings.py`, 8 in `test_tree_migration
.py`, and three holdback tests deleted. `app/attempts.py` is the new module; migrations
57–60 add `live`, `state_after`, `world_state_after`, derive the after-snapshots, and
split every `variants` list into rows.

**The baseline test did not have to move, and neither did `test_retry_variants.py`.**
Both pass unmodified. SP4 was *permitted* to change the variant-count semantics and it
turned out nothing observable needed changing — which is the strongest form the pass
condition could have taken, and worth knowing before SP6 asks for the same licence.

Seven things worth not rediscovering:

- **A coordinate needs a `live` flag, and the branch clause is where it belongs.**
  Siblings share `(branch_id, depth)`, so the lineage clause alone returns all of them
  and the story tells itself twice. `Path.clause` adds `Action.live` for actions (and
  only for actions — memories have no siblings to lose to), which means no read of the
  story had to learn that retries exist. `app/attempts.py` is the only code that looks
  past it.
- **The prompt has to move with the flag or retry becomes a storage multiplier.** A
  `context_snapshot` is ~163 kB of prompt that every attempt at a turn shares, and the
  old JSON list existed precisely to store it once. Giving each sibling row a copy would
  have undone that. So the invariant is: the assembled prompt lives on the attempt the
  story tells, and a superseded one keeps only its own slices (`ATTEMPT_KEYS` — the
  world-state delta, the script report, the raw reply). Measured on the pre-tree
  600-action fixture, migrated: **700 rows for the same 600-turn story, and the prompt
  archive byte-identical at 0.50 MB.**
- **The holdback was not quite unnecessary — it was the wrong repair.** The plan said
  retry stops mutating rows so nothing goes stale. Not so: siblings share a coordinate,
  and the mark and the memory both name the *coordinate*, so replacing what a turn says
  still invalidates them. What made the holdback deletable is that the right repair
  already existed — `forget_node` plus a rewind, which undo and delete have called since
  SP3. Retry and a sibling switch now call it too, and the holdback (`settled_count`,
  `settled_after`, `settled_story_actions`, `newest_settled`) is gone. **A memory can
  now cover the newest action**, which it never could before.
- **`state_after` needed the same flush guard `place_action` has.** The migration
  derives every existing row's outcome from the next row's `state_before`, and the tip's
  from the adventure's live state — but a row written by a fixture or a script after
  that has nobody to derive from, and the failure mode is undo silently leaving the
  scoreboard where it was. `tree.stamp_outcome` runs from `before_flush` beside
  `place_new_nodes`. It writes the truth as of the flush: a writer that changes no state
  between two nodes leaves the same state behind both.
- **Switching attempts hands the caller a different row id**, because that is what an
  attempt being a node *means*. The endpoints are addressed by any attempt at the turn
  rather than by the live one, so a client holding a stale id still asks about the right
  turn — but `Play.jsx` matched the reply against `updated.id` and had to be changed to
  match against the action it asked about. One line, and SP7 removes the pager anyway.
- **Deleting a turn deletes its attempts.** A discarded attempt is only reachable
  *through* its coordinate, so leaving it behind would leave a row nothing can name and
  no read can see. `delete_turn` is that rule in one place, called by undo and by
  delete-an-action, and it works whichever attempt's id the caller happens to hold.
- **Siblings share the legacy `index`.** They are takes on one turn, so two rows now
  carry one index — which `max_action_index` (a maximum, not a count) survives, and
  which is what lets the v1 export fold a group back into a `variants` array. Export is
  now the only producer of that shape anywhere; nothing in the database holds one.

Measured: index **1.8 kB**, page load **62.7 kB**, one turn **734.8 kB** — the first two
byte-identical to SP1's and SP3's, the turn up 1.0 kB (0.14 %) for the `live` column
across a 346-row read. Migrations 57–59 each rewrite every row of `actions` and 60
inserts one per discarded attempt, so **this deploy owes a `VACUUM FULL actions;`** —
the SP1 one is still owed too, and one vacuum after this deploy settles both.

### SP5 — Fork on continue

Playing past a non-head sibling promotes it: a `branches` row with `parent_branch_id`,
`fork_depth`, and a `lineage` computed once from the parent's. Nothing is copied.
`adventures.head_*` moves. **The cooldown clock must not advance on a re-run of the same
turn** — the one item still open above.

**Verify:** a fork creates exactly one branch row and copies no actions; lineage is
correct and capped at each `fork_depth`; both branches read independently; switching
restores the right script/world state; the cooldown test from `test_worldstate.py`
still holds across a retry.

**Done, 2026-08-18** (branch `sp5-fork-on-continue`). **365 tests green**, the 347 from
SP4 plus 18 in `test_branch_forking.py`. `tree.fork` is the whole of it; three endpoints
(`GET /branches`, `POST /branches/{id}/switch`, `POST /actions/{id}/fork`) are what SP7's
tree view will be drawn on.

**Where the promotion happens, and why not where the plan said.** The plan put it on the
*next turn*: attempts stay leaves, and one becomes a branch when a turn is played past
it. Promoting the winner then means moving a row off the branch the reader is standing
on and leaving that branch to pick a new node for the depth — it disturbs a story nobody
asked to change. The same divergence, seen from the other side, promotes the attempt you
are *leaving for*: `POST /actions/{id}/fork` gives the discarded attempt a branch and
moves the head to it, and the line it leaves is untouched. Branch count is identical
either way — one per divergence somebody actually built on — and one of the two never
rewrites a story in place. Without it, the losing attempts would also be unreachable
forever, since only the tip can be switched: fork-on-continue alone is a one-way door.

Six things worth not rediscovering:

- **A fork must not move the derived work, and it was about to.** The first cut moved the
  memories at the forked coordinate onto the new branch and re-anchored the cursors that
  named it. Both are wrong, and for the same reason: a memory describes whichever attempt
  was *live* at that coordinate, which is the one staying on the parent. The right answer
  needs no code at all — the lineage caps the parent at `fork_depth`, one depth short of
  it, so the memory is simply out of range from the fork, invisible to both the retrieval
  clause and `Path.depth_on`. The block is summarized again, from the text this branch
  actually tells.
- **Depth had to stop following `index`.** They agreed until now. `index` is
  adventure-wide (the v1 bundle is keyed on it) so on a story forked at depth 6 after
  twenty turns it hands the next node depth 21 and leaves a fourteen-deep hole in the
  middle of a path — which every windowing estimate then has to work around.
  `next_depth` is `head_depth + 1`; `place_action` still derives depth from index for
  fixtures and imports, which is what that default is for.
- **Undo has to stop at the fork.** It reads the newest two nodes on the path and deletes
  the turn they make up — and on a fresh branch the second of them is borrowed from the
  parent, whose story also contains it. The guard is on the row's own `branch_id`, not on
  the fork depth, because that is the fact that decides it.
- **The cooldown clock came out right for free.** It lives in `_meta.last_changed` inside
  the world state, and the world state is restored from the tip's `world_state_after` on
  every switch — so each branch carries its own clock without anything knowing there is
  one. The carried-over open item (a retry must not advance it) is SP4's reused depth,
  and both are pinned by tests.
- **The session does not autoflush**, and `fork` read the sibling group after moving the
  node out of it — so the move had not been written and the node was renumbered straight
  back into the group it had just left. Read the group first. (`autoflush=False` is
  deliberate, in `database.py`; anything in this phase that mutates then queries the same
  rows has to order itself by hand.)
- **`/fork` has to be idempotent before it is anything else**, because a fork leaves the
  promoted attempt alone on its branch: a repeated call — a double click, a retried
  request — would otherwise be told the turn it just forked has nothing to fork to. The
  "already the story" case is answered before the shape of the turn is looked at.

Measured, on a 40-turn story forked **twenty** times against the same story flat:
21 branches, 140 rows, an 80-action story, and a page load of **31,652 B against
31,433 B (1.007×)**. A branch costs **103 B** of id, parent, fork depth and cached
ancestry. No migration, no vacuum.

**One gap, deliberately left for SP6.** A forked adventure has no honest `v1` export —
the format has one story and there are two — so export emits every branch's turns
interleaved by index, which reads as a mangled story rather than as lost data. SP6's v2
bundle fixes it, and SP7 is where a player first gets any way to fork at all, so the
order those two ship in is the order that matters.

**Also known, and not fixed here:** the two cursors are one pair on the adventure, so
switching branches makes the mark on the branch being left unreadable from the new one
(`Path.depth_on` answers "nothing covered", which is the safe direction — redo the work,
never skip it). Switching back and forth therefore re-summarizes. Per-branch cursors are
the fix if it ever matters; it costs AI calls, not correctness.

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
`variant_count`, followed by `VACUUM FULL actions;`. **Also `adventures.memory_cursor`
and `summary_cursor`** — unread since SP3, kept only so a rolled-back build resumes from
a real number. They are on `adventures`, so dropping them costs no vacuum.

**And `actions.state_before` / `world_state_before`**, unwritten and unread since SP4 for
the same reason: a rolled-back build still finds a real snapshot on every row it wrote
itself. They are deferred JSON on `actions`, so they go in the same rewrite as `index`
and cost nothing extra.

`variant_count` and `variant_index` are the two to check before dropping: SP4 left them
as a maintained cache of the sibling group's shape, because the pager reads both for
every row of a page and must not pay a query per turn to get them. They are dead only
once SP7's tree view has replaced the pager.

**Verify:** full suite; egress ceilings; a measured before/after size, aggregates only.

## Standing constraints

- **No production data is read at any point in this phase.** Migrations, the e2e
  baseline and every egress measurement run on local SQLite and the synthetic `--keep`
  fixture. If a real Postgres is ever needed for a write path, it is a throwaway
  database whose name contains `stress`/`scratch`, and it is asked for first.
- **Any migration that rewrites `actions` is followed by `VACUUM FULL actions;`** on the
  direct endpoint, not `-pooler`. SP1, SP4 and SP8 each rewrite every row. **As of
  2026-08-18 two are owed** (SP1's and SP4's) and neither has been deployed; one vacuum
  after the SP4 deploy settles both.
