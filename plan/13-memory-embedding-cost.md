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
2. **Migration 38 — `memories.embedding_blob` (`LargeBinary`).** Backfill in Python
   (`struct.pack(f"<{n}f", *vec)`); the conversion cannot be expressed in portable SQL, so
   unlike migration 36/37 this one does pay a one-time 4 MB read. Drop the old JSON column
   in a follow-up migration once verified, not in the same one.
3. **Read path.** `retrieve_memories` queries `memories` directly with
   `forgotten = false AND embedding_blob IS NOT NULL` in **SQL**, not Python. Unpack with
   `struct`/`numpy`.
4. **Vector cache.** Keyed by `adventure_id`, invalidated on memory create, evict and
   delete. Must survive the retry/undo paths that prune memories.
5. **Capacity default 200 → 80.** Existing adventures inherit it, so adventure 25 evicts
   on its next turn — check that the eviction path is sane at scale before shipping.
6. **Infinite scroll upward in `Play.jsx`** for the remaining 423 KB page load of a
   finished adventure — the last open item from round two. Load the newest turns, fetch
   older ones as the reader scrolls up.

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

- Harness: one turn on adventure 25, memory bank **on**, before and after. Target is
  3,024 kB → ~600 kB cold, ~0 warm.
- Re-run the round-two shapes with an embedding model configured, so the 200-action
  playthrough number is finally honest.
- The existing `test_egress.py` guard must still pass — nothing here should touch the
  deferred action columns.
