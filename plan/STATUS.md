# Where things stand

Read this first when picking the project back up. Updated at the end of a working
session; the per-phase plan files hold the detail, this holds the thread.

**Last updated: 2026-08-16.**

---

## Pick up here

**`plan/13-memory-embedding-cost.md`, step 6 — infinite scroll upward in `Play.jsx`.**

Opening a finished 200-action adventure fetches **426.7 kB** in one response, and after
this session's work that is comfortably the largest single read in the app — a turn is
now 122 kB, Insights 118 kB, the Memories drawer 24 kB. The backend already has the
windowing primitives (`context/history.py`: `tail_range`, `slice_`, `count`), and
`GET /adventures/{id}/actions` exists. What is missing is a paged shape for it and a
`Play.jsx` that loads the newest turns and fetches older ones as the reader scrolls up.

Watch for: the story is a flat list today, and **the story tree replaces it**
(`plan/14-phase-story-tree.md`). Paging that reads by *position from the end* survives
that change; paging that assumes `Action.index` is a dense 0..n sequence does not.

After that: the tree itself. Its design is settled in `plan/14`; nothing about it has
been built.

---

## What happened on 2026-08-16

Four commits, all on the egress work that has to land before the tree.

### 1. A byte meter, in the repo this time — `7ee5cee`

`backend/tools/dbmeter.py` + `backend/tools/stress_session.py`.

```
cd backend
.venv/Scripts/python.exe -m tools.stress_session
.venv/Scripts/python.exe -m tools.stress_session --shapes turn --repeat 2
.venv/Scripts/python.exe -m tools.stress_session --no-embeddings   # the old blind spot
```

It counts **bytes at the DBAPI cursor**, not queries — both egress blowouts this project
has had were one query fetching a column nobody read, and a statement count showed
nothing wrong in either. It drives a production-shaped synthetic adventure through the
real routes with only the LLM and the embedding endpoint faked.

**The memory bank is ON by default and that is the whole point.** The previous harness
ran without an embedding model configured; embedding providers are BYOK-only by
construction, so `retrieve_memories` returned early every time and the heaviest read in
a turn never happened. `--no-embeddings` reproduces that deliberately — the gap is 29x.

It calibrates against the two figures measured directly on production: 426.7 kB for a
200-action page load against 423 KB, and 3,258.7 kB for one turn against 3,153 kB.
It runs on SQLite, so treat absolutes as production-*shaped* and compare before/after.

### 2. Packed float32 embeddings — `c568648`

Migration 38 + `app/vectors.py`. A 1536-dimension vector as a JSON list is ~31 KB; the
same numbers as float32 are 6,144 bytes. **Not a precision trade** — the endpoints
compute in float32 and render that into JSON, so converting back is bit-exact. Nothing
re-embeds, no API calls.

The backfill is the one in `migrations.py` that cannot be portable SQL, so it comes
through Python, batched. Migration SQL can now be a `{dialect: sql}` map (BLOB vs BYTEA
have no common spelling).

### 3. Ranking the bank without reading the bank — `b7e53ae`

`retrieve_memories` walked `adventure.memories`, loading every row *with its vector*. It
now asks SQL which memories are in play (an id and a flag per row), ranks against
vectors held in process, and fetches text only for the top-K it picks.

Two more callers were doing the same thing, and the production SQL could not see either:
`_evict_over_capacity` walked the bank to count it, `_embed_pending` walked it to find
rows with no vector. A played turn cost **6.4 MB**, not the 3.2 the plan assumed.

| shape | before | cold | warm |
|---|---|---|---|
| one turn | 3,258.7 kB | 723.4 kB | **122.3 kB** |
| `run_post_turn` | 3,139.1 kB | 0.7 kB | 0.7 kB |
| Insights | 3,223.7 kB | 117.9 kB | 117.9 kB |
| Memories drawer | ~3.1 MB | 23.7 kB | 23.7 kB |

A played turn is turn + post-turn: **6.4 MB → 123 kB**, 52x.

Migrations 39/40 add `memories.embedded`, migration 41 drops the capacity default
200 → 80 for rows still on the old default.

---

## Things worth remembering

**The vector cache needs no invalidation callbacks, and that is why it is safe.** A
stored vector can only change through `memorybank.set_vector`, which drops that one
entry. Anything that *removes* a memory from play — eviction, deletion, pruning, an edit
clearing the vector — falls out of the catalogue query, and entries missing from the
catalogue are dropped on the next read. So there is no hook anyone can forget to call.
It is in-process and assumes one worker, which is what the deploy runs.

**Weigh new columns in bytes, not rows.** The comment on `Memory.embedding` said "fine
at bank sizes of a few hundred" and was wrong by the only measure that mattered: a few
hundred JSON vectors is ten megabytes, fetched fresh every turn.

**A deferred column needs a cheap flag beside it.** `memories.embedded` exists because
once the vector is deferred, every "is this embedded?" check becomes a 6 KB lazy load,
once per row down the Memories drawer. Exactly the same shape as `actions.variant_count`
beside `actions.variants`. Expect to need this for any future heavy column.

**Any egress measurement must run with an embedding model set.** This is the second
time that omission has hidden the biggest number in the room.

---

## Still open from `plan/13`

- **Step 6, infinite scroll upward** — the pick-up item above.
- **Query-count / byte assertions per endpoint**, extending `tests/test_egress.py`
  against production-sized fixtures. `dbmeter` is importable from tests (`from tools
  import dbmeter`) and was built with this in mind; nothing uses it there yet.
- **Explicit column projections on read paths**, so the next heavy column is opt-**in**.
  Done for the memory paths, not as a general rule.
- **Drop `memories.embedding`** (the JSON column) in a follow-up migration. It is still
  written by `set_vector` and read by nothing, kept so a rollback finds the vectors.
  `tests/test_memory_retrieval.py` has a guard asserting nothing selects it.

Deliberately not taken: moving `context_snapshot` out of the database (~$0.02/mo, costs
nothing on reads now that it is deferred), and pgvector (breaks the SQLite dev parity
this codebase protects on purpose).

---

## Running things

```
cd backend
.venv/Scripts/python.exe -m pytest tests/          # 225 tests
.venv/Scripts/python.exe -m tools.stress_session   # egress report
```

Port 8000 is shared with the job-pipeline app, which will squat it and silently shadow
the AI-DnD API — free it before running the backend, or move the vite proxy.
