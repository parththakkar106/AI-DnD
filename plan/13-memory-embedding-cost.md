# 13 — Memory-bank embedding cost (round three of the egress work)

**Goal:** stop every turn fetching the entire memory bank's embeddings. Measured at
**3,024 KB per turn** on adventure 25 against **129 KB** for everything else a turn
reads — the memory bank is ~96% of a turn's database traffic, and it is fetched fresh
every single turn to pick `memory_top_k = 5` memories.

Found 2026-08-16 while designing the story tree (see `14-phase-story-tree.md`), because
memory retrieval is the one read a tree **cannot** window — it is long-range recall by
design, so it always spans the full path. That makes this the cost floor of a turn under
the tree, which is why it lands first.

## The measurement (production, Neon SQL editor)

```sql
SELECT count(*), avg(json_array_length(embedding))::int,
       avg(length(embedding::text))::int,
       pg_size_pretty(sum(length(embedding::text))::bigint)
FROM memories WHERE embedding IS NOT NULL;
--  134 memories | 1536 dims | 30,971 bytes each | 4,053 kB total
```

| adventure | active | fetched | per turn |
|---|---|---|---|
| 25 | 100 | 100 | **3,024 kB** |
| 21 | 18 | 18 | 545 kB |
| 12 | 12 | 12 | 363 kB |
| 20 | 4 | 4 | 121 kB |

Break-even against the rest of a turn is **4.2 memories**, i.e. about action 25. Every
adventure past that is dominated by this.

`active == fetched` everywhere: nothing has been evicted yet, so the Python-side
`forgotten` filter currently costs nothing. It becomes a real leak the moment eviction
starts.

## Why round two missed it

`retrieve_memories` needs `settings.embedding_model`, and embedding providers are
**BYOK-only by construction** — they never touch the demo key. So the public demo never
embeds anything, and the round-two stress harness (which had no embedding model
configured) measured the turn loop with its heaviest read switched off. The 23.0 MB
figure for a 200-action playthrough is the memory-bank-**off** number; with it on,
adventure 25 is closer to 300 MB.

**Rule going forward: any egress measurement must run with an embedding model set.**

## Root cause

`memorybank.py:208`

```python
candidates = [m for m in adventure.memories if not m.forgotten and m.embedding]
```

Walks the relationship, so every memory row for the adventure loads with its embedding.
`embedding` is `Mapped[list]` on a `JSON` column — 1536 floats serialised as text is
~31 KB. Cosine ranking happens in Python (a deliberate choice, documented at
`models.py:144`), so all of it must cross the wire. The comment sized it by **count**
("fine at a few hundred") rather than by **bytes**.

Not the same bug as migration 36/37 — the column is not a repeating group and there is
no denormalisation to fix. It is a *format* problem plus a *fetch-frequency* problem.

## Decisions (settled 2026-08-16)

- **Packed float32, keep 1536 dimensions.** 31 KB → 6 KB, a straight 5x, with **zero
  retrieval-quality risk**. Explicitly rejected dropping to 512/768 dims: the size fix
  plus the cache makes the extra 3x unnecessary, and it would have meant re-embedding.
- **No re-embedding.** Dimensions are unchanged, so the migration is a pure format
  conversion of the 134 existing rows — read the JSON, write packed bytes, no API calls.
  One-time 4 MB read.
- **In-process cache alongside the size fix**, not sequenced after it. Turns for one
  adventure arrive back-to-back, so a dict keyed by adventure id takes steady-state cost
  to ~0. 100 vectors as float32 is 600 KB of RAM — negligible.
- **`memory_bank_capacity` 200 → ~80.** Taken on *quality* grounds as much as cost:
  ranking 200 memories to pick 5 dilutes retrieval. Note this will start evicting on
  adventure 25 immediately (it sits at 100).
- **Not pgvector.** It is the structural answer and would keep vectors in the database
  entirely, but it breaks SQLite dev parity — which the codebase protects deliberately
  (`context/history.py:42`, the `replace()`/`trim()` dialect dance). Revisit only if the
  bank grows past what Python cosine can handle.

## Work, in order

1. **Rebuild the byte-meter harness — in the repo this time.** `backend/tools/dbmeter.py`
   + `stress_session.py`. The originals lived outside the repo and are gone. **Default it
   to running with an embedding model configured**, since that omission is precisely what
   hid this finding. Do this first so every item below is measured, not assumed.
   **Done 2026-08-16** — see the baseline below.
2. **Migration 38 — `memories.embedding_blob` (`LargeBinary`).** **Done.** Backfill in Python
   (`struct.pack(f"<{n}f", *vec)`); the conversion cannot be expressed in portable SQL, so
   unlike migration 36/37 this one does pay a one-time 4 MB read. Drop the old JSON column
   in a follow-up migration once verified, not in the same one.
3. **Read path.** **Done.** `retrieve_memories` queries `memories` directly with
   `forgotten = false AND embedding_blob IS NOT NULL` in **SQL**, not Python. Unpack with
   `struct`/`numpy`. Same for `_evict_over_capacity` and `_embed_pending`, which walk the
   same relationship for a count and for the unembedded rows (see the baseline above).
4. **Vector cache.** **Done.** Keyed by `adventure_id`, bounded to 8 adventures. It
   turned out to need no invalidation callbacks at all — see below.
5. **Capacity default 200 → 80.** **Done** (migration 41, only rows still on the old
   default). Eviction checked at scale first: trimming a 100-memory bank to 80 costs
   0.8 kB and eight statements, and reads no vectors at all.
6. **Infinite scroll upward in `Play.jsx`** for the remaining 423 KB page load of a
   finished adventure — the last open item from round two. Load the newest turns, fetch
   older ones as the reader scrolls up.

## Baseline from the harness (2026-08-16)

`python -m tools.stress_session`, 200 actions × 4 KB, 100 memories × 1536 dims:

| shape | fetched | memories' share |
|---|---|---|
| `GET /adventures` (index) | 0.7 kB | — |
| `GET /adventures/{id}` (page load) | 426.7 kB | — |
| `POST /adventures/{id}/actions` (one turn) | **3,258.7 kB** | 96% |
| `GET /adventures/{id}/context` (Insights) | 3,223.7 kB | 97% |
| `run_post_turn` (background) | **3,139.1 kB** | 100% |

It reproduces both production figures independently — 426.7 kB against the measured
423 KB page load, 3,258.7 kB against 3,024 + 129 kB for a turn, and 31.4 KB per
embedding against 31.0 KB. `--no-embeddings` reports 112.0 kB for the same turn, so
the round-two blind spot is now a **29x** gap anyone can see in one flag.

**Two findings the SQL measurement could not have shown**, both the same root cause
in a different caller:

- **`run_post_turn` fetches the whole bank again**, every turn. `_evict_over_capacity`
  walks `adventure.memories` to count the active ones, and `_embed_pending` walks it to
  find the unembedded ones. So a played turn actually costs ~6.4 MB, not 3.2 — the
  original estimate was half the real number.
- **Insights pays it a third time**, on a page the player can open repeatedly without
  spending a turn.

So step 3 below is not just `retrieve_memories`: **every walk of
`adventure.memories` has to go**. `_evict_over_capacity` wants a count and an ordering,
`_embed_pending` wants rows where `embedding IS NULL` — neither needs a single vector,
and both are pure SQL.

## After (2026-08-16, same harness, same fixture)

| shape | before | after (cold) | after (warm) |
|---|---|---|---|
| `POST .../actions` (one turn) | 3,258.7 kB | 723.4 kB | **122.3 kB** |
| `run_post_turn` | 3,139.1 kB | 0.7 kB | 0.7 kB |
| `GET .../context` (Insights) | 3,223.7 kB | 117.9 kB | 117.9 kB |
| `GET .../memories` (drawer) | ~3,138 kB | 23.6 kB | 23.6 kB |

A played turn is turn + post_turn: **6,398 kB → 123 kB** once warm, a 52x cut. The
targets above were ~700 kB cold and ~130 kB warm, so both were met.

Steps 2–5 landed together, because they are one deployable unit: the columns are no
use unless something reads them, and deferring them breaks the old readers. Two
additions the plan did not anticipate:

- **`memories.embedded`, a boolean beside the blob** (migration 39/40). Once the
  vectors are deferred, every "does this have an embedding?" check becomes a lazy
  load — an N+1 of 6 KB reads down the Memories drawer. Same shape as
  `actions.variant_count` beside `actions.variants`, and the same reason.
- **The cache needs no invalidation callbacks.** Vectors only ever change through
  `set_vector`, which drops the one entry; everything that *removes* a memory from
  play leaves the catalogue query, and entries missing from the catalogue are dropped
  on the next read. So eviction, deletion and pruning need no hooks and cannot be
  forgotten. Vectors are held as `array("f")` — 6 KB each, matching the column;
  a list of Python floats would have been eight times the plan's RAM estimate.

Remaining: **step 6, infinite scroll upward in `Play.jsx`.** The page load is
unchanged at 426.7 kB and is now the largest single read in the app.

## Guardrails to add with this work

- **Query-count / byte assertions per endpoint**, extending the `test_egress.py` idea:
  assert an endpoint issues at most N queries and fetches under X KB against
  production-sized fixtures. This class of bug is invisible at ten rows.
- **Explicit column projections on read paths.** List endpoints name the fields they
  need rather than loading whole entities, so the next heavy column is opt-**in**. This
  is the structural version of what `deferred=True` does by hand.
- **Row-width review rule.** Any new large column justifies itself or goes out-of-line.
  `actions` now carries five JSON columns.

Deliberately **not** taken: moving `context_snapshot` out of the database. It costs
nothing on reads now that it is deferred, and storage is ~$0.02/mo. Revisit only if
backups or storage start to hurt.

## Verification

- Harness: `python -m tools.stress_session`, memory bank **on**, before and after,
  against the baseline table above. Target for the turn shape is 3,258 kB → ~700 kB
  cold, ~130 kB warm (the cache leaves only what a turn reads besides the bank).
  `run_post_turn` should fall to roughly nothing: neither of its two walks needs a
  vector at all.
- Re-run the round-two shapes with an embedding model configured, so the 200-action
  playthrough number is finally honest.
- The existing `test_egress.py` guard must still pass — nothing here should touch the
  deferred action columns.
