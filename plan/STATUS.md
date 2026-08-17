# Where things stand

Read this first when picking the project back up. Updated at the end of a working
session; the per-phase plan files hold the detail, this holds the thread.

**Last updated: 2026-08-18.**

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

**`plan/14-phase-story-tree.md`, SP4 — variants become sibling nodes.** SP0 (the
regression contract and the `--rich` fixture), SP1 (schema, migration, and the writer that
keeps new rows on the tree), SP2 (the branch clause: every action read selects on
`(branch_id, depth)` through `app/context/lineage.py`) and SP3 (memories hang off nodes,
and both marks are `(branch_id, depth)` through `app/context/cursors.py`) are done and
green; **nothing is deployed yet**. SP4 is where retry stops rewriting a row and writes a
sibling leaf at the same depth instead, where `state_before`/`world_state_before` become
*after* snapshots, and where the legacy `variants` JSON is migrated into rows. It is also
the first subphase allowed to move the baseline test, and only for
`variant_count`/`variant_index` semantics.

**The schema is live in code but not on production.** When this ships, the deploy needs
one `VACUUM FULL actions;` on the direct (non-`-pooler`) endpoint afterwards — SP1's
migration rewrites every row, and SP4's does it again. SP3's own migrations touch
`adventures` only and need no vacuum. See the 144 MB lesson at the top of this file.

Three things to carry into it:

- **The holdback dies in SP4 and nowhere earlier.** `settled_story_actions` exists
  because retry mutates a row in place; it survived SP3 as the `- 1` inside
  `memorybank.settled_after`. Retry stops mutating in SP4, which is the only point at
  which removing it does not reopen the bug it was written for.
- **Anything derived attaches to the node that produced it.** A memory now does
  (`tree.attach_memory`), and so do both marks. A sibling leaf is a node, so whatever SP4
  derives per attempt hangs off the attempt — and `memorybank.forget_node` is what
  withdraws it when the node goes.
- **Weigh new columns in bytes.** `actions` is already the table that fills the disk.
  `tests/test_egress.py` has byte ceilings now — they will tell you.

**After any migration that rewrites `actions`:** one `VACUUM FULL actions;`. That is the
lesson of the 144 MB above — a rewrite doubles the table and only a `VACUUM FULL` gives
it back. Phase 14's migration rewrites every row.

**There is a 600-action adventure to test against now** — `--keep`, below. The tree's
frontend work lands on the same scroll path that has still never been driven by hand, so
drive it before rewriting it.

---

## What happened on 2026-08-18 — the tree, SP3

The memory bank stopped counting. `memory_cursor` and `summary_cursor` were positions in
the story — "the first twelve actions are covered" — and a position moves when an action
in front of it is deleted, so it silently starts covering one it has never read. Both are
now node anchors, `(branch_id, depth)`, through the new `app/context/cursors.py`; a memory
hangs off the node whose block it ends on; and retrieval selects through the branch
clause, so a memory made on one branch never reaches a prompt on another. **330 tests
green**, and the SP0 baseline still passes unmodified. Branch `sp3-node-cursors`.

Three things to carry forward:

- **Most of the work was deleting, and that was the test of the design.** The plan listed
  four pieces of cursor machinery to remove and the expectation was that each would come
  back in depth-shaped form. None did. `count_after(41)` is well defined with node 41
  deleted and unchanged by anything deleted in front of it, so `note_action_removed`,
  `_rewind_cursors_to_index`, `position_of_index` and the every-pass clamp in
  `run_post_turn` all became nothing at all. **If a mark still needs correcting when the
  story changes, it is still a position.** The one thing a delete still does is withdraw
  what the node *produced* — `memorybank.forget_node`, a lookup on `(branch_id, depth)`
  where `prune_dangling_memories` was a scan that could only notice damage afterwards.
- **A NULL is not a small depth, and it nearly cost a feature.** A hand-written memory
  summarises no node, so it has a branch and no depth; every ancestor entry in a lineage
  clause is capped `depth <= fork`, and NULL fails that test. A memory somebody typed
  would have disappeared at the first fork after they typed it, with no error anywhere —
  just a prompt that stopped mentioning it. `Path.clause(unanchored=True)` names that case
  explicitly, and actions are deliberately not given it.
- **Reading the whole ancestry for recall is free.** Retrieval cannot be windowed — that
  is the point of it — so the clause names every branch in the lineage. Two 84-action
  stories with 14 memories each, one flat and one forked twenty times: **1,807 B against
  1,823 B**. Twenty-two branch terms cost nothing, because the clause is not what crosses
  the wire. Index (1.8 kB), page load (62.7 kB) and turn (733.8 kB) are unmoved.

## What happened on 2026-08-17, part five — the tree, SP2

Every read of an action now goes through one module. `app/context/lineage.py` turns a
branch's stored lineage into the OR-of-ranges that is "this story", and history, paging,
the newest-action lookups, the index screen and the scripting history API all select
through it. Ordering moved from `index` to `depth`. **317 tests green**, and the SP0
baseline still passes unmodified, which was the pass condition. Branch `sp2-branch-clause`.

Three things to carry forward:

- **A read-side invariant needs a write-side floor.** From SP2 a row without a branch is a
  row no read can see, and it fails by *disappearing*. Wiring every writer was not enough,
  because the SP0 baseline and eleven other fixtures write actions straight to the database
  and never call `place_action` — and the baseline may not be edited. `tree.place_new_nodes`
  now runs from `Session.before_flush`, so nothing can be written unplaced. That is a
  better invariant than the one SP1 shipped, and it was the contract that forced it.
- **The SQLAlchemy identity map is weak, and that is a performance cliff.** Resolving the
  head branch once per node re-read the row from the database for every node in a flush —
  201 SELECTs to write 200 actions, and a 25 % slower suite (36 s → 45 s, back to back).
  Nothing about the results changed; only a stopwatch could see it. Hoist the lookup out
  of the loop and hold the reference for the length of the call: 2 SELECTs, and the suite
  back within noise of SP1. Pinned by a test that counts the reads rather than the
  seconds — this machine's timings drift ~20 % between runs.
- **Clause count is bounded by the window, and it is now measured.** A story forked 20
  times reads its newest 32 actions naming *one* branch, for 1.07× what an unforked story
  of the same length costs. Reading the tail widens the lineage only when a deleted action
  leaves the estimate short.

The 600-action `--keep` fixture — a genuine pre-tree database — was migrated and then
driven over HTTP: index 1,840 B, page load 64,149 B (both unchanged), and scrolling to the
start took 9 pages and saw every action exactly once.

## What happened on 2026-08-17, part four — the tree, SP0 and SP1

No behaviour change, and none intended: a linear story is a tree with one branch, so
every adventure reads exactly as it did. **297 tests green** (259 before the phase
started, 283 with SP0's contract, 297 with SP1's migration tests).

**SP0** built `tests/test_story_tree_baseline.py` — 24 tests driving the product over
HTTP, asserting only on API responses — and `tools.stress_session --rich`, a correctness
fixture beside the scale one. Both on branch `phase-14-story-tree`.

**SP1** put the tree in the schema, on branch `sp1-tree-schema`: a `branches` table,
`branch_id`/`depth` on actions and memories, `head_branch_id`/`head_depth` on adventures,
migrations 46–52 with a server-side backfill, and `app/tree.py` for the write side.
Nothing reads any of it yet. Four things worth not rediscovering:

- **A schema needs its writer in the same subphase.** No migration will ever visit a row
  written after it ran, so columns backfilled today and populated-on-write next week leave
  a hole exactly the width of one deploy. `app/tree.py` stamps every new node, including
  the ones `seed_demo.py` and the stress fixture write — a fixture built by `create_all`
  is stamped LATEST and no migration ever touches it.
- **SQLite will not drop a column a foreign key names.** Two tests simulated an old
  database by rewinding the *stamp* while `create_all` left the new columns in place; that
  works until the next `ADD COLUMN` lands, and then it fails on a duplicate column. Every
  `ADD COLUMN` migration is now idempotent (`migrations._column_already_there`), which is
  the `IF NOT EXISTS` SQLite has no syntax for. A true pre-migration fixture has to drop
  and rebuild the tables from frozen DDL, which is what `test_tree_migration.py` does.
- **Two mutually-referencing tables cannot both carry the foreign key.** `create_all`
  refuses to order the cycle, and its escape hatch (`use_alter`) needs an ALTER SQLite
  does not have. `adventures.head_branch_id` is a plain integer and a documented cache.
- **The suite went 20 s → 38 s, and it is not the app.** One new table plus one index adds
  ~47 ms to a `create_all`/`drop_all` pair on SQLite (DDL fsync), and nearly every test
  does one. Measured, not guessed. Egress is unmoved: `branches` is 0.1 kB of a 733.5 kB
  turn, and the page-load and index shapes are byte-identical to the numbers above.

## What happened on 2026-08-17, part three

No behaviour change. A way to get a long adventure in front of a browser, because the
one open gap needed a subject and there wasn't one.

**`tools.stress_session --keep PATH`.** The harness already built a production-shaped
600-action adventure and then threw it away with the temp file; `--keep` writes it
somewhere durable and makes the app able to serve it. Two edits are needed for that, and
both are the kind of thing that costs an hour to rediscover:

- **`create_all()` does not stamp the schema version.** `bootstrap()` reads a
  populated-but-unstamped database as ancient and replays every migration against a
  schema that already has the columns. `--keep` stamps `PRAGMA user_version` to
  `LATEST_VERSION`.
- **The fixture's user is a registered one.** In local mode `get_current_user()` looks
  for the row with `email IS NULL` and `is_guest` false, so without clearing the email
  the app opens on an empty library and nothing owns the 600 actions.

`--keep` is read before argparse exists (`_early_keep`), because where the database
lives has to be settled before `app.database` is imported — the same constraint the
`AIDND_STRESS_DATABASE_URL` block at the top of the module already lives under. SQLite
only; combining it with a Postgres target is rejected rather than half-honoured.

Verified: the fixture boots with no manual step, `action_count` 600, the first payload
carries 60 actions, and `before_id` walks back through 9 more pages to the start — 600
seen, `has_more` false at the end. 259 tests pass.

**Snapshots can be shrunk for this.** `--snapshot-bytes 2000` keeps the file at ~2.5 MB
instead of ~140 MB. `context_snapshot` is deferred and never reaches the browser, so it
changes nothing about what scrolling exercises — but do not shrink it when *measuring*,
where it is most of the point.

**Port 8010, not 8000.** Covered below, and now printed by `--keep` itself.

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

- **Nothing has ever exercised the scroll in a browser** — still true, but there is now
  something to exercise it *on*: `--keep` builds a 600-action adventure the app will
  serve (see 2026-08-17 part three). The subject is no longer the excuse; only the
  looking is left. This is the one real gap, and
  it has already cost something: re-reading that path after shipping turned up a bug
  where loading earlier turns scrolled *past* them to the end of the story, worst on
  the short-window case the button exists for (fixed, PR #2). One bug found by reading
  means reading is not a substitute. Either scroll a long adventure by hand, or add a
  vitest + jsdom harness — that would have caught this one. jsdom has no layout, so the
  scroll-position arithmetic still needs eyes.
- **`ACTION_PAGE = 60` is a guess.** It should be a page or two of reading. If loading
  older turns feels like it interrupts, that is the number to move
  (`routers/adventures.py`). One data point: at 600 actions it takes the window plus
  **nine** more pages to reach the start, which is a lot of button presses for anyone
  going back to the beginning.
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
.venv/Scripts/python.exe -m pytest tests/          # 330 tests (~55s)
.venv/Scripts/python.exe -m tools.stress_session   # egress report (SQLite)

# Same harness against a real Postgres. The target must be a THROWAWAY database
# — this writes a synthetic adventure, and it refuses any name without
# 'stress'/'scratch' in it.
AIDND_STRESS_DATABASE_URL=postgresql://…/stress_scratch \
    .venv/Scripts/python.exe -m tools.stress_session
```

**A long adventure to scroll**, instead of a temp file the report discards. The
snapshots are shrunk because they never reach the browser — 2.5 MB rather than 140 MB —
and `--shapes list` skips the measurement work the fixture does not need:

```
cd backend
.venv/Scripts/python.exe -m tools.stress_session \
    --keep ./scroll_fixture.db --snapshot-bytes 2000 --shapes list

AIDND_DB_PATH=$PWD/scroll_fixture.db \
    .venv/Scripts/python.exe -m uvicorn app.main:app --port 8010
cd ../frontend && AIDND_API_PORT=8010 npm run dev      # → localhost:5173
```

Everything in it is synthetic and no real adventure is read. `*.db` is gitignored, so
the fixture never lands in a commit.

**A fixture to check correctness against, rather than bytes.** The measuring fixture
leaves every column it does not weigh at its default, which turns out to be exactly the
set a story tree has to migrate — `state_before`/`world_state_before` NULL on all 600
rows, no RPG scenario, no adventure scripts, both cursors 0, and retry attempts whose
text is byte-identical with `variant_index` always 0. `--rich` fills in those and only
those:

```
cd backend
.venv/Scripts/python.exe -m tools.stress_session --rich --actions 30 --memories 12
```

Prefer it small — it exists for variety per row, not for rows. **Its byte figures are
not comparable to a plain run**, and it does not replace the scale fixture, which still
holds the egress ceilings.

On Windows the report's box-drawing characters crash the default cp1252 console;
prefix with `PYTHONIOENCODING=utf-8`.

Port 8000 is shared with the job-pipeline app, which will squat it and silently shadow
the AI-DnD API — free it before running the backend, or move the vite proxy with
`AIDND_API_PORT`, which is what the `--keep` recipe above does.
