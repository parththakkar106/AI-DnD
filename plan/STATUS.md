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

**`plan/14-phase-story-tree.md`, SP7 — the frontend.** SP0–SP6 are done and green
(**381 tests**); **nothing is deployed yet**. The tree is complete everywhere except the
screen: a retry writes a sibling node, continuing from a discarded attempt forks a branch,
and a backup carries the whole thing (`ai-dnd-adventure-v2`, with the v1 reader kept so
existing bundles still import). What is left is the frontend (SP7) and dropping the legacy
columns (SP8).

**SP7 is the release gate, and it is unscoped.** `VariantPager` comes out, a spatial tree
view replaces it, and branch management — switch, rename, delete-with-confirm — is a hard
dependency rather than a nice-to-have, because nothing auto-prunes and storage otherwise
grows without limit. `api.js` gains `GET /branches`, `POST /branches/{id}/switch` and
`POST /actions/{id}/fork`, which SP5 built for exactly this.

**Drive the 600-action `--keep` fixture in a browser by hand.** That is SP7's own verify
line and the standing open gap in this project: the scroll path has never been driven by
hand and has already hidden one bug. A vitest + jsdom harness covers the prepend
arithmetic, but jsdom has no layout, so scroll position still needs eyes.

**The schema is live in code but not on production.** When this ships, the deploy needs
one `VACUUM FULL actions;` on the direct (non-`-pooler`) endpoint afterwards — SP1's
migration rewrites every row and SP4's rewrites it three times more, so **two vacuums are
owed and one run settles both**. SP3's, SP5's and SP6's changes need none (SP5 and SP6 add
no migration at all). See the 144 MB lesson at the top of this file.

Three things to carry into SP7:

- **The bundle is the one thing here a migration can never reach.** `app/bundle.py` owns
  both formats and nothing else knows either. Its rule — *carry what was chosen, never
  what is derived* — is worth borrowing anywhere else state has to leave the database.
- **`variant_count` and `variant_index` die with the pager.** SP4 left them as a
  maintained cache of the sibling group's shape because the pager reads both for every
  row of a page. They are dead the moment the tree view replaces it, and SP8 drops them.
- **Nothing on the screen has ever seen a second branch.** Forking has no UI, which is
  why the v1-export gap could be left open through SP5 — SP7 is the subphase that makes
  a fork reachable, so it is also the one that makes every branch-shaped bug reachable.

And one known cost, not a bug: the two memory marks are a single pair on the adventure,
so switching branches makes the mark on the branch being left unreadable from the new one
and that ground is summarized again. `Path.depth_on` answers "nothing covered", which is
the safe direction. Per-branch cursors are the fix if it ever matters.

**After any migration that rewrites `actions`:** one `VACUUM FULL actions;`. That is the
lesson of the 144 MB above — a rewrite doubles the table and only a `VACUUM FULL` gives
it back. Phase 14's migration rewrites every row.

**There is a 600-action adventure to test against now** — `--keep`, below. The tree's
frontend work lands on the same scroll path that has still never been driven by hand, so
drive it before rewriting it.

---

## What happened on 2026-08-18, part three — the tree, SP6

The backup learned the tree. `ai-dnd-adventure-v2` carries branches, the fork point each
one left its parent at, which attempt at every turn is the story, and what each node left
behind — that last one because it is what a branch switch puts back, and a bundle that
imported a tree nobody could switch inside would be a backup of the wrong thing. The v1
*reader* stays: those files are already on disk. **381 tests green**, 16 of them new in
`test_bundle_v2.py`. Branch `sp6-bundle-v2`, no migration, no vacuum.

The gap SP5 left is closed — a forked adventure now has an honest export — so the
ordering constraint that has governed the last two subphases is discharged, and SP7 is
free.

Three things to carry forward:

- **Carry what was chosen, never what is derived.** The head branch, the fork points, the
  live flags and the anchors are decisions, and they are in the file. `lineage`, the head
  depth, the legacy `index` and the variant ordinals are computed from those, so they are
  rebuilt on import instead. A bundle is a text file anybody can edit, and a derived field
  shipped beside its source is a chance for the file to contradict itself in a way no read
  reports. It also turns "is the round trip lossless?" into a testable question: every
  omitted field is reconstructed, and the tests assert the reconstruction.
- **Check the shape before creating the row.** A node naming a branch the file does not
  list is a 400 raised by a pure function, not a half-written adventure. The failure this
  phase exists to end is a story that goes quiet, and a half-applied import is exactly
  that.
- **The tree is free; the outcomes are what cost.** On the 600-action fixture the bundle
  goes from 587 kB to 911 kB, and *all* of it is `state_after`/`world_state_after` at
  489 B a node — the coordinates themselves save 57.5 B a node against v1's
  turn-and-variants shape. Twenty forks add 660 B. 4.3 % of the import body cap at
  production's longest adventure.

Paid for once, and worth not repeating: a new measuring script imported `app` before
`tools.stress_session`, which is what redirects `AIDND_DB_PATH` at a throwaway file. It
failed by *working* — the first run seeded a synthetic user into the local `data.db` and
printed good numbers; the second tripped over the unique email. **A harness that decides
where the database lives has to be imported before anything that reads it.**

## What happened on 2026-08-18, part two — the tree, SP4 and SP5

A retry stopped rewriting a row, and a story learned to go two ways at once.

**SP4** (branch `sp4-sibling-nodes`, **347 tests green**). Every attempt at a turn is now
its own node at the same `(branch_id, depth)`, with a `live` flag naming the one the story
tells; `app/attempts.py` owns the group. The JSON repeating group on `actions.variants` is
read one last time — by migration 60, which writes it out as the rows it always described
— and then goes unread. The state snapshots turned around with it: an action carries what
it left *behind* (`state_after` / `world_state_after`) rather than what it started from,
because attempts at one turn share a starting position and differ exactly in their
outcome. **The SP0 baseline and `test_retry_variants.py` both pass unmodified**, which SP4
was permitted to change and did not need to.

**SP5** (branch `sp5-fork-on-continue`, **365 tests green**). Taking the story down an
attempt the line has already moved past gives that attempt a branch of its own, forked at
the depth just before it. One row inserted, one row moved, nothing copied. Measured on a
40-turn story forked twenty times against the same story flat: 21 branches, 140 rows, an
80-action story, page load **31,652 B against 31,433 B (1.007×)**, and a branch costs
**103 B** of cached ancestry.

Four things to carry forward:

- **The holdback was the wrong repair, not an unnecessary one.** The plan said retry would
  stop mutating rows so nothing could go stale, and that is not quite true — siblings
  share a coordinate and the mark names the coordinate, so replacing what a turn says
  still invalidates the memory covering it. What made `settled_story_actions` deletable is
  that the *right* repair already existed: `forget_node` plus a rewind, which undo and
  delete have called since SP3. Retry and a sibling switch make it too. **If a mark still
  needs correcting when the story changes, correct it — do not decline to make the mark.**
- **A fork must move nothing derived, and the first cut moved it all.** Memories at the
  forked coordinate were being carried onto the new branch and the cursors re-anchored.
  Both wrong, for one reason: a memory describes whichever attempt was *live* there, and
  that one stays on the parent. The right answer needs no code — the lineage caps the
  parent one depth short of it, so it is simply out of range from the fork, and the block
  is summarized again from the text this branch actually tells. **When a coordinate system
  already answers a question, adding bookkeeping to answer it again is how it gets two
  answers.**
- **Storage arrangements have invariants too.** A `context_snapshot` is ~163 kB of prompt
  that every attempt at a turn shares — the JSON list existed to store it once. Giving
  each sibling row a copy would have made retry a permanent multiplier on the biggest
  column in the database. So the prompt moves with the `live` flag and a superseded
  attempt keeps only its own few hundred bytes. Migrating the real 600-action fixture:
  **700 rows for the same 600-turn story, prompt archive byte-identical at 0.50 MB**, and
  index/page-load/turn egress unmoved at 1.8 kB / 62.7 kB / 734.8 kB.
- **`autoflush=False` is set in `database.py`**, and it bit once: `tree.fork` read the
  sibling group *after* moving the node out of it, so the move had not been written and
  the node was renumbered straight back into the group it had just left. Anything in this
  phase that mutates rows and then queries the same rows has to order itself by hand.

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
.venv/Scripts/python.exe -m pytest tests/          # 365 tests (~100s)
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
set a story tree has to migrate — the per-action state snapshots identical on all 600
rows, no RPG scenario, no adventure scripts, both cursors 0, and retry attempts whose
text is byte-identical with the first always live. `--rich` fills in those and only
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
