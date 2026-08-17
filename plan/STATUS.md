# Where things stand

Read this first when picking the project back up. Updated at the end of a working
session; the per-phase plan files hold the detail, this holds the thread.

**Last updated: 2026-08-17.**

---

## The live URL is not the one in render.yaml

**Production is `https://ai-dnd-1gmp.onrender.com`.** Render appended a suffix to the
`ai-dnd` service name in `render.yaml`, and plain `ai-dnd.onrender.com` belongs to a
different, suspended service that answers 503 with "suspended by its owner" — which is
easy to mistake for this deploy being down. The authoritative link is the one the
project page points at (`docs/index.html`), not the service name in the blueprint.
`GET /api/health` on the real host returns `{"ok":true}`.

---

## Shipped and live, 2026-08-17

PRs #1 and #2 are merged and deployed. Verified against the running service:
`/api/health` 200, the adventure payload carries `action_count`, `/actions` returns
`{actions, total, has_more}` and accepts `before_id`. The app booting at all is proof
migrations 42–45 ran — `bootstrap()` executes at import, so a failed migration means no
service. The deployed JS bundle hashes to `index-4vjcKxkv.js`, which is what this tree
builds, so the frontend live is exactly this code.

**Measured 2026-08-17, and then vacuumed again.** The earlier `VACUUM FULL actions;`
predated migrations 42–45, which rewrote every row — so by the time anyone looked, the
database was **144.2 MB**, well above the 99.6 MB it started from and nowhere near the
~53 MB projection. Nothing was wrong with the compression. Nothing had reclaimed the
space it freed.

```
before: database 144.2 MB, actions 131.4 MB
VACUUM (FULL, ANALYZE) actions -- 5.5s
after:  database  65.0 MB, actions  52.1 MB
```

**79.2 MB reclaimed in 5.5 seconds**, against a 512 MB tier. `actions` now occupies
52.1 MB holding 50.4 MB of live column bytes, so there is essentially no bloat left.
`/api/health` answered `{"ok":true}` immediately after. The projection was right all
along.

**Read the sizes from the column sums, not from `n_live_tup`.** The chunk count on the
TOAST relation said ~88 MB live and implied ~40 MB was reclaimable; the actual figure was
double that. `n_live_tup` is an estimate left over from the last ANALYZE, and after a
migration rewrites the table it is stale in the direction that makes bloat look smaller.
`sum(octet_length(col))` per column is the honest number.

**Where the bytes are in `actions`** — one column, and it is not close:

| column | rows | live bytes | per row |
|---|---|---|---|
| `context_snapshot` | 467 | **47.5 MB** | 104.1 kB |
| `variants` | 91 | 1.1 MB | 12.9 kB |
| `text` | 945 | 0.8 MB | 0.9 kB |
| `world_state_before` | 787 | 0.7 MB | 1.0 kB |
| `world_delta` + `reasoning` + `state_before` | — | 0.2 MB | — |
| total | | **50.4 MB** | |

`context_snapshot` is 94% of the table. The per-action state columns a tree would touch
are rounding errors, which is the useful thing to know before phase 14 adds more of them:
**adding columns beside `state_before` is cheap; adding anything shaped like a context
snapshot is not.**

`VACUUM FULL` takes an ACCESS EXCLUSIVE lock — the app blocks on `actions` for the
duration. 5.5 s at this size, but it grows with the table. Run it on the **direct**
endpoint, not `-pooler`: through transaction pooling it is unreliable.

**Aggregates only, and ask first.** Real users are on this database. Counts,
`octet_length` sums and catalog sizes answer every sizing question this project has
needed; nothing requires reading a row of anyone's story.

---

## Pick up here

**`plan/14-phase-story-tree.md` — the tree itself.** `plan/13` is finished. Its design
is settled in `plan/14` and nothing about it has been built.

Two things from the egress work are worth carrying into it:

- **Paging already anticipates the tree.** `action_window` in `routers/adventures.py`
  anchors on an action id and orders by comparing `Action.index`, never by treating
  index as a position. A branch changes which actions are on the path, not how two of
  them order, so the anchor survives; anything counting offsets would not.
- **Weigh new columns in bytes.** A tree adds parent/branch columns to `actions`, which
  is already the table that fills the disk. `tests/test_egress.py` has byte ceilings
  now — they will tell you.

**After any migration that rewrites `actions`:** one `VACUUM FULL actions;`. That is the
lesson of the 144 MB above — a rewrite doubles the table and only a `VACUUM FULL` gives
it back. Phase 14's migration rewrites every row.

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

## What happened on 2026-08-17, part two

Everything left open in `plan/13` closed, plus two bugs that fell out of doing it.

| shape | before today | after |
|---|---|---|
| page load, 600 actions | 606.0 kB | **62.6 kB** — and no longer grows with the story |
| adventures index, 6 fat adventures | 469.7 kB | **0.3 kB** |
| `context_snapshot` on disk | ~89 MB | 47.5 MB measured (3.2x) |
| database total | 99.6 MB | **65.0 MB measured**, after the second vacuum |

**One follow-up after the merge** (PR #2). Prepending older actions changes `actions`,
and the bottom-pinning effect watches `actions` — so unless a scroll had already
un-pinned the view, loading earlier turns jumped to the newest one instead. A prepend
now clears the pin explicitly. Found by re-reading the path, not by running it.

**The story is a window now.** `GET /adventures/{id}` returns the newest 60 actions and
`action_count`; older pages come from `GET /{id}/actions?before_id=`. Anchored on an
action, never an offset — an offset counted back from the newest shifts every older
position the moment a turn lands, which is exactly when someone is scrolling. `Play.jsx`
prepends and restores scroll position in a `useLayoutEffect`, before paint.

**`context_snapshot` is compressed** (migrations 43–45, `app/compression.py`) via a
TypeDecorator, so every call site still reads and writes a dict. Verified end to end on
a throwaway Neon database: 720,864 B of JSON to 204,293 B of bytea, every row equal.

**The JSON vector column is gone** (migration 42) — and dropping it exposed that
changing your embedding model had silently stopped re-embedding the bank since
migration 38. The settings route cleared the dead column and left `embedded` true, so
`_embed_pending` never saw those rows and retrieval kept ranking against the old
model's vectors. Nothing reported it: `cosine` returns 0.0 on a width mismatch.
`tests/test_embedding_model_switch.py`.

**Byte ceilings exist** (`tests/test_egress.py`), including one test whose only job is
to prove the ceilings would catch something.

**List responses name their columns.** The index was loading whole Adventure entities —
seven text and JSON columns, ~15 kB a row — to render a title and a snippet.

## What happened on 2026-08-17, part one

No new behaviour — a verification pass on what shipped the day before, because every
number in the section above had been measured on SQLite against a synthetic fixture.
Full write-up in `plan/13` under "Verified on production".

**It holds.** `schema_version` is 41 on the live Postgres with `embedding_blob bytea`
and `embedded boolean` present, so migration 38's dialect map is correct against a real
server. The backfill is complete (134/134). The packed vectors are **5.04x** smaller
than the JSON on real data — 30,971 → 6,144 bytes a memory, as predicted.

**SQLite was not lying.** `tools.stress_session` can now target Postgres via
`AIDND_STRESS_DATABASE_URL`, and every shape agrees within 0.5% — the warm turn is
121.1 kB on Postgres against 122.3 kB on SQLite, with `memories` down to 1.7 kB of it.
Run it against a **throwaway** database only; the harness writes, so it refuses any
target whose name does not contain `stress` or `scratch`.

**Two corrections came out of it**, both above: the page-load model has the wrong
shape (too heavy per action, far too short), and the storage ceiling was never costed.

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

**Production has real users on it now. Measure it without reading it.** Counts,
`sum(octet_length(...))` and `pg_total_relation_size` answer every sizing question
asked so far, and none of them return anyone's story, memory text or email. When a
real Postgres is needed for a *write* path, create a throwaway database beside the real
one and drop it after — never point a harness at the production database.

**`octet_length` is the egress number, not the on-disk number.** Postgres TOAST
compresses big JSON — `context_snapshot` is 150.8 MB uncompressed but ~89 MB stored —
and decompresses before sending. Size reads with `octet_length`, size the storage bill
with `pg_total_relation_size`, and do not mix them up.

---

## `plan/13` is closed

All six of its open items landed on 2026-08-17, and are live. What is left is not from
that plan:

- **Nothing has ever exercised the scroll in a browser.** This is the one real gap, and
  it has already cost something: re-reading that path after shipping turned up a bug
  where loading earlier turns scrolled *past* them to the end of the story, worst on
  the short-window case the button exists for (fixed, PR #2). One bug found by reading
  means reading is not a substitute. Either scroll a long adventure by hand, or add a
  vitest + jsdom harness — that would have caught this one. jsdom has no layout, so the
  scroll-position arithmetic still needs eyes.
- **`ACTION_PAGE = 60` is a guess.** It should be a page or two of reading. If loading
  older turns feels like it interrupts, that is the number to move
  (`routers/adventures.py`).
- ~~Post-vacuum sizes unmeasured~~ — measured 2026-08-17, and the vacuum that mattered
  was run then too. 65.0 MB. See the top of this file.
- **Anyone who switched embedding models has a stale bank.** The bug is fixed, but
  those memories only re-embed as the post-turn pass reaches them, which costs an
  embedding call each. Nothing forces it; playing does.

Deliberately not taken: moving `context_snapshot` out of the database entirely
(compressing it bought the same runway for a much smaller change), and pgvector (breaks
the SQLite dev parity this codebase protects on purpose).

---

## Running things

```
cd backend
.venv/Scripts/python.exe -m pytest tests/          # 259 tests
.venv/Scripts/python.exe -m tools.stress_session   # egress report (SQLite)

# Same harness against a real Postgres. The target must be a THROWAWAY database
# — this writes a synthetic adventure, and it refuses any name without
# 'stress'/'scratch' in it.
AIDND_STRESS_DATABASE_URL=postgresql://…/stress_scratch \
    .venv/Scripts/python.exe -m tools.stress_session
```

On Windows the report's box-drawing characters crash the default cp1252 console;
prefix with `PYTHONIOENCODING=utf-8`.

Port 8000 is shared with the job-pipeline app, which will squat it and silently shadow
the AI-DnD API — free it before running the backend, or move the vite proxy.
