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

- **Export/import bundle format** (`routers/adventures.py:1008-1112`) encodes `variants`
  and `variantIndex`. Needs a versioned format change and a legacy reader.
- **`retry_of.index` reuse** stops the world-state cooldown clock advancing on a re-run of
  the same turn. Whatever replaces it must preserve that.
- **Phased or single migration?** Undecided. A tree touches the memory bank, the context
  builder, undo/retry and the UI at once, which argues for phasing behind a flag.
- **What the branch-management UI actually looks like** — unscoped, and it gates release.
