# Where things stand

Read this first when picking the project back up. Updated at the end of a working
session; the per-phase plan files hold the detail, this holds the thread.

**Last updated: 2026-08-31.**

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

## What happened on 2026-08-31 — the persona, and what the summarizer is told

**`plan/18-persona-and-memory-quality.md` is the writeup; it is on
`claude/ai-dnd-memories-summarization-3muo98`, not yet merged.** Two changes, both green
at 610 tests.

**The protagonist now has a name.** An adventure carries `persona_name`,
`persona_pronouns` and `persona_desc` (migrations 74-76), and the player's stat block
renders as `Kaelen (player): hp 100/100` instead of `You: hp 100/100`. The paths do not
change — a path carrying the persona's name would break the moment a player renamed
their character, because `_history_text` replays stored deltas holding literal
`player.hp` strings. Empty name means the app behaves exactly as before, so no backfill.
Driven in a browser, 21/21 checks.

**The summarizer used to be told nothing.** It got six actions of second-person prose
and no cast, no setting, and no instruction about what person to write in. It now gets a
cast brief built from the story cards — which already cover the schema NPCs, because
`scenario_card_specs` turns every one of them into a card at adventure creation.

**Keyword matching alone was not enough, and only running it showed that.** Built to the
plan first, the brief for "She grabs your arm" listed the protagonist and nobody else:
the block that most needs a cast is exactly the one written in bare pronouns. Matched
cards now come first and the rest of the roster is filled with the other `character`
cards.

**Running it against a real model found a second fault.** "1-2 plain sentences" is not a
length — the same model wrote 34 words for one block and 105 for the next, and
`memory_top_k` injects five every turn. `MEMORY_MAX_WORDS = 50` states it; the same
blocks then came back at 32 and 58, and a second run on a fresh story came back at 54
and 55 against controls of 89 and 107. The A/B harness is `backend/tools/memory_ab.py`,
it drives the real provider through `tools/claude_shim.py`, and both transcripts are in
`plan/18-appendix-memory-ab-run.md` and `-run-2.md`.

**Read one of those findings narrowly.** Run 1 produced two control memories in two
different persons, which is the reported complaint exactly; run 2's controls were both
"the player", so that drift is something a model sometimes does, not always. What holds
across both runs is that none of the four control memories names the protagonist and all
four treatment memories do.

**Still unmeasured: whether a weaker model complies.** The run used a Claude model
through the shim. The app talks to an OpenAI-compatible endpoint, and
`worldstate/parse.py` tolerates trailing commas because free models emit them. Point
`memory_ab.py --endpoint` at the real provider to find out.

---

## Pick up here

**`plan/17-refactor.md` is the active phase.** It carries its own progress table, which
is the first thing to read when you pick the work back up. It runs in six stages, and
SP8 below is stage 3 of it. Nothing in it changes what the app does.

**`plan/14-phase-story-tree.md`, SP8 — drop the legacy columns.** SP8 was gated on the
tree being proven live, and it now is: SP9 merged, and production answers `/api/health`
with the tree schema in place. SP8 drops `index`, `variants`, `variant_index`,
`variant_count`, the two legacy cursors and the two `*_before` snapshots. Check that
nothing still reads `variant_count`/`variant_index` before dropping them, and note this
is the migration shape that rewrites toasted values, so it owes one `VACUUM FULL actions;`
on the direct (non-`-pooler`) endpoint afterwards.

Ahead of that, `plan/16-world-state-refusals.md` is closed. Its fixes were driven in a
browser on 2026-08-28, first on the demo model and then on Claude Sonnet through the
local shim, and all five changes work. One item in it is still open and is the next
world-state job: **Milo's three Pokemon share one `active_hp` stat**, so a switch leaves
the newcomer at 0 HP and every later hit is refused. Give them their own NPC entries,
which also removes the `initial == max` shape from that demo.

**SP9 and SP10 are both on `main`, despite what earlier notes here said.** The branches
`sp7b-take-pager` and `sp10-memory-bank-eviction` still exist and still read as unmerged
to `git branch --no-merged`, because the work landed as squashes. Check the code, not the
branch list: `actions.parent_id` in `models.py` is SP9, and commit `c0cd6fa` is SP10.

**Driving it found two bugs the suite could not have.** The pager did not appear until the
page was reloaded — a retry's reply is the second take of its turn, and the SSE stream
builds its own `ActionOut`, so it was the one payload that never got the annotation. And
retaking a player turn gave `> You > You ...`, because the editor is seeded with the stored
text, which is already formatted. Both fixed, both with regression tests. **Neither was
reachable from any test that existed, and the frontend still has no test runner** — which
is the standing reason this project keeps finding UI bugs by hand.

**State was checked against the running app, not just asserted.** On the HP-script demo, a
path crossing *three* branches carried exactly the damage of the nine AI nodes on it and
none of the 66 points sitting on takes those branches never told. `test_take_state.py`
pins the same guarantee with a script that adds ten gold a turn; three of its five tests
fail if `roll_back_before` is removed.

**What SP7 got wrong, in one line: a chip meant two different things.** At the tip it
switched; further back it only previewed, and taking that line needed a second button. The
meaning depended on where the reader was standing. SP9 replaces it with `‹ 2/4 ›` that only
ever steps, a fork button on every turn, and a rule that decides everything else:

> Reading a take is free and tells the server nothing. **Writing below one is what makes
> the branch.**

Three things came out of building it that outlive the subphase:

- **Takes are grouped by parent, not by coordinate.** A take forked onto its own branch
  leaves the (branch, depth) its siblings are at and would read `1/1` beside their `1/3`.
  `actions.parent_id` fixes it, and is read for nothing else — one indexed lookup, never a
  walk, no read of the story changed. See SP9 in the phase plan for why the alternative
  (fork points as nodes rather than depths) was rejected.
- **`delete_turn` meant "every take at this coordinate".** Once the group spans branches,
  undo reached onto another branch and deleted a take belonging to a line nobody asked
  about. Anything that reads a take group and then *writes* needs to ask whether it means
  the turn or the coordinate.
- **The adventure GET does not build `ActionOut`.** It hands the window to the
  relationship with `set_committed_value` and lets Pydantic walk it. Patching every place
  that builds `ActionOut` therefore misses the one path every page load takes — worth
  remembering for the next field added to a page.

**The vacuum ran on 2026-08-18, and the projection was wrong in the useful direction.**

```
before: database 82 MB, actions 69 MB (heap 5184 kB, indexes 184 kB)
after:  database 71 MB, actions 58 MB (heap 1760 kB, indexes  96 kB)
```

**11 MB reclaimed**, against a "mid-hundreds of MB" guess. The heap is 1.7 MB and
everything else is TOAST: `context_snapshot` is 94% of the table and lives out of line, so
when SP1's and SP4's migrations updated small per-row columns each UPDATE wrote a new heap
tuple and **reused the existing TOAST pointer** — Postgres only copies a toasted value when
that value itself changes. Four rewrites bloated a 1.7 MB heap, not a 58 MB table, and the
arithmetic closes (3.3 heap + 0.1 index + 7.6 TOAST = the 11 MB the database gave back).

**So the rule keeps its cost estimate, not its size.** *After a migration that rewrites
`actions`, one `VACUUM FULL`* still stands — but bloat scales with **the heap**, whenever
the migration only touches small columns. The 144 MB incident was different because that
rewrite genuinely moved every toasted value. **SP8 will be the 144 MB shape, not this one:**
it drops `variants` and the two `*_before` snapshots, which are the toasted kind. And note
`ALTER TABLE … DROP COLUMN` is metadata-only in Postgres — it frees nothing by itself, and
the space comes back only at the next `VACUUM FULL`.

Growth since the previous vacuum (65.0 MB / 52.1 MB) is real: migrations 61–62 and a day
of play, not bloat.

**SP8 is gated on the tree being proven live, and it is not.** It drops `index`,
`variants`, `variant_index`, `variant_count`, the two legacy cursors and the two
`*_before` snapshots. `variant_count` / `variant_index` are the ones to watch: SP7's
attempt chips still read both, so SP8 has to move the chips onto the sibling group before
it drops them. Everything else has been unread since SP3/SP4.

**Deploy before SP8, not after.** SP7 is a natural release: the phase is usable from the
screen for the first time, and dropping columns is the one step that cannot be rolled
back by redeploying the previous build.

**The schema is live in code but not on production.** When this ships, the deploy needs
one `VACUUM FULL actions;` on the direct (non-`-pooler`) endpoint afterwards — SP1's
migration rewrites every row and SP4's rewrites it three times more, so **two vacuums are
owed and one run settles both**. SP3's, SP5's, SP6's and SP7's changes need none (SP5 and
SP6 add no migration; SP7's migration 61 touches `branches`, a handful of rows per
adventure). See the 144 MB lesson at the top of this file.

Three things to carry forward:

- **The bundle is the one thing here a migration can never reach.** `app/bundle.py` owns
  both formats and nothing else knows either. Its rule — *carry what was chosen, never
  what is derived* — decided SP7's naming too: `branches.name` is stored because a player
  picked it, and an unnamed branch is drawn from its fork depth rather than given a
  generated label that would go stale when a branch before it is deleted.
- **A one-line subphase spec can hide a schema change.** SP7 read as "plus the UI for
  switch/rename/delete"; two of those three had no backend at all. Check the routes exist
  before believing a spec that says "frontend".
- **The spatial node map was deliberately not built.** SP7 shipped a rail instead, on the
  grounds that a per-node map is a second windowing problem at 600 nodes. It is a
  standalone feature whenever it is wanted, and it needs no new endpoint — the rail and a
  map both draw from `GET /branches`. **A branch-level map was built on 2026-08-20** (see
  below); the per-node version is still not, and the windowing argument still stands
  against it.

And one known cost, not a bug: the two memory marks are a single pair on the adventure,
so switching branches makes the mark on the branch being left unreadable from the new one
and that ground is summarized again. `Path.depth_on` answers "nothing covered", which is
the safe direction. Per-branch cursors are the fix if it ever matters.

**After any migration that rewrites `actions`:** one `VACUUM FULL actions;`. That is the
lesson of the 144 MB above — a rewrite doubles the table and only a `VACUUM FULL` gives
it back. SP8's migration rewrites every row.

**The scroll gap is closed.** It was driven by hand on the 602-action `--keep` fixture
during SP7 — three prepends, 5 px of drift, no throw-to-the-end. What is still missing is
an automated version; see SP7's entry in `plan/14`.

---

## What happened on 2026-08-28 — world state against a real model

**The refusal loop ran end to end for the first time.** `plan/16` shipped a correction
that is injected into the next turn's prompt when the engine clamps or rejects a change,
and nothing had ever observed it working. Turn 3 of the Pokemon demo clamped to nothing,
turn 4's assembled prompt carried the note verbatim, and the model's next delta was
correct. The full record, with the four turns and their chips, is at the end of
`plan/16-world-state-refusals.md`.

**Two failures blamed on the code were the model.** `graveler_defeated` never firing and
`world.turn` never moving both survived four playtests on the free demo model, with the
instructions present and correct in the assembled prompt. Both worked on the first try
against Sonnet. When an instruction is provably in the prompt, the next thing to change
is the model, not the wording.

**There is now a permanent way to test against a real model.**
`backend/tools/claude_shim.py` serves an OpenAI-compatible endpoint backed by the local
`claude` command line tool, so a demo can be played on a real model with no API key.
About $0.04 a turn against the subscription. The README documents it under **Connect a
model**.

**Renaming a seed scenario used to strand its old row.** `seed.py` matches a seed file to
its scenario by title, so changing a title inserted a second public scenario and left the
first orphaned and public forever. This is the same failure this file already records for
"Road to the Champion". Seed files now carry `previous_titles`, and the rename lands on
the existing row. Verified: the Pokemon demo kept its id.

**A seeded scenario nobody claims is now deleted on boot.** `previous_titles`
stops a rename stranding a row; the sweep removes the ones already stranded, which
retires "[Demo] Road to the Champion" without a console. Only rows with a NULL owner
and `is_public` are reachable, and `adventures.scenario_id` is `ON DELETE SET NULL`, so
an adventure started from a deleted demo keeps its story and loses only the cover art.
A seed file that fails to parse, or an empty seed directory, skips the sweep.

**Every new guest is given a pre-played adventure.** `app/starter.py` copies a shipped
export bundle into each new guest account. The point is the first screen: real turns with
their world-state chips, including a refused change and a milestone, before spending any
of the daily demo turns. The row building inside `POST /adventures/import` moved to
`bundle.materialize` so both callers share one writer. The copy is linked back to its
demo scenario by title, because an adventure has no art of its own and a bundle cannot
carry an id that means anything in another database.

**An SVG data URI is not usable as scenario art.** `app/images.py` accepts raster formats
only, deliberately, because SVG can carry script and the bytes are served from the app's
own origin. A rejected value is stored but yields an empty `image_url`, which fails
quietly. The Pokeball is a 3.2 kB PNG, drawn by `backend/tools/make_pokeball.py` with
`zlib` alone.

---

## What happened on 2026-08-21 — visit analytics

The hosted demo can now answer whether anyone is using it. `/analytics` is a dashboard —
visitors, pages, referrers, countries, devices, which shared scenarios get played, turns and
demo-key spend, API and turn errors, and a funnel from *visited* to *played a turn* to
*signed up*. **Built, green (497 tests), driven by hand against a synthetic 90-day fixture.
Committed and pushed to `main` on 2026-08-22 as `041f9e2`, so Render is deploying it.**

**The one thing left to do is not in the repo.** `AIDND_ANALYTICS_EMAILS` is `sync: false`,
so the blueprint cannot fill it: until it is set in the Render dashboard the dashboard is
invisible to everybody, including the person who built it. Collection runs regardless — only
the view is gated — so the counters are filling in the meantime and nothing is lost by
setting it late.

**It is gated on its own allowlist, `AIDND_ANALYTICS_EMAILS`** — not `AIDND_POWER_USERS`.
An unmetered tester is not automatically someone who sees the traffic numbers. The route
404s and the nav link is absent for everyone else, same treatment as AI Chat.

**The counters are anonymous; the access log beside them is not, on purpose.** A visitor in
`analytics_daily`/`analytics_visitor_days` is `HMAC(secret, "visitor:<user id>")` truncated
to 32 chars — one-way, so those two tables cannot be joined back to `users`, and keyed, so no
client can compute one. Story content never reaches that module. The operator's own visits
are not counted there (multi-user only — excluding them locally would leave the page
permanently empty on the machine it is developed on).

**`accesslog.py` is the identifying half, added the same week.** `access_events` records
sessions, sign-ins, registrations and failed attempts with address, email (or `Guest #n`),
country and device, read on an "Access log" tab of the same page and behind the same owner
gate. It is a separate module and a separate table so the anonymity of the counters stays a
property of the code rather than a convention. Three things it leans on: the address comes
from `limits.client_ip` — now public, and the only place that decides which hop to trust, so
a spoofed `X-Forwarded-For` cannot forge a row; `user_id` carries **no foreign key** and
`who`/`is_guest` are snapshots, so a row outlives the guest cleanup that deletes the account;
and session rows are thinned to one per day per address, since `/auth/me` runs on every page
load. Nothing is purged — that was the deliberate choice. The published docs describe the
analytics generally and do not enumerate this.

**A visit is a write and never a read.** Counts accumulate in a process-local dict and flush
every 60s as UPSERTs into `analytics_daily` — a generic `(day, metric, label) -> hits`
counter — plus one row per visitor per day in `analytics_visitor_days` for the funnel flags.
Every dashboard query is a `GROUP BY` returning tens of rows however much traffic sits
behind it. Deliberate, given §2.5: adding a feature that reads rows per request would have
undone the egress work.

**No migration was needed.** Both tables are new, and `bootstrap()` calls `create_all` on
existing databases too — the same route `branches` took in Phase 14. Nothing was appended to
`MIGRATIONS`, so `LATEST_VERSION` is still 64.

Three things worth remembering out of building it:

- **The funnel counts people, not clicks.** A player who starts six adventures is one person
  who started an adventure. That is the entire reason the per-visitor-day table exists; its
  flags only ever turn on, and `is_new` is settled by the first write of a visitor's first
  day and never updated after.
- **A failed turn is an HTTP 200 with a bad ending.** The status-code middleware cannot see
  one, so a demo whose model started refusing every request would look perfectly healthy.
  `turn_error` is counted in a new `turn_error()` helper that all five SSE error paths in
  `_generate_turn` now go through.
- **The tests run on SQLite; production is Neon.** A flush that raises is caught and logged,
  so a dialect mistake in the UPSERTs would have been invisible until the dashboard quietly
  stayed empty. `test_the_upserts_compile_for_postgres` compiles both statements against the
  Postgres dialect without connecting to one.

**Still not verified: the narrow-screen layout.** The CSS follows the existing `max-width:
720px` block (single-column grids, funnel label above its bar) but `resize_window` is
ignored on a maximized Chrome, and the app sends `frame-ancestors 'none'` so it cannot be
checked in a sized iframe either. Desktop was driven by hand at 1568px.

**`docs/guide.html` is now behind `docs/GUIDE.md`,** which gained §3.6. Nothing in the repo
regenerates it.

---

## What happened on 2026-08-20 — the branch map

The Branches panel gained a **⌗ See the tree** button opening a full-screen map: one
horizontal lane per branch, running from the moment it left its parent to the moment it
ends, joined to the parent by an elbow at the fork. Clicking a lane selects it; the
footer switches, renames or deletes it. **Merged to `main` and pushed on 2026-08-20**
(`c8e081e`), so it is on its way to Render with the rest of the branch. **440 backend tests
pass, unchanged — this is frontend-only.**

**It is a branch map, not the node map SP7 refused, and that is the whole reason it was
cheap.** Lanes are bounded by branch count, not by node count, so the 600-node windowing
problem never appears: it draws from the same single `GET /branches` the rail already
made, and reads nothing else.

New: `frontend/src/branches.js` (the tree maths — `branchLabel`, `orderBranches`,
`headLineage`, `layoutTree`, `momentTicks`) and `frontend/src/BranchMap.jsx`. `Play.jsx`
lost its private copies of the first two; the panel and the map now label and order a
branch through the same functions, so a branch cannot be called two things by the two
views. The three operations stayed in `BranchPanel` and are passed down, and `run` now
answers whether it worked so neither view clears a half-typed name on a refusal.

`tools/tree_fixture.py` is the counterpart to `tools/branch_fixture.py`: four branches at
three different fork depths, one forked off a fork. **It is the whole of the map's
coverage** — the frontend still has no test runner — and it exists to be looked at.

Three things found by driving it, none of which a test could have seen:

- **`clientWidth` is not `contentRect.width`.** Seeding the measured width from
  `clientWidth` counts the canvas padding the ResizeObserver leaves out, so the first
  paint drew an svg 24 px wider than its box. And the observer's *initial* observation
  did not arrive at all here, so dropping the seed and keeping only the observer left the
  map never drawing. Both halves are needed, and the seed has to subtract the padding.
- **Only the name was being clipped.** The meta line under it was not, so a lane that
  forks late ran its text off the right edge. Both are clipped now, and a lane starting in
  the right third hangs its labels back over the fork instead — its row is its own band,
  so there is nothing to the left to collide with.
- **The delete rule was in the server and in the map, but not in the list.** The panel
  offered Delete on a branch the head was forked from and answered with a toast from the
  server's 400. `headLineage` is the client's copy of that rule and both views now use it.
  The server stays the authority.

Also worth recording: **the 390 px iframe trick in the older notes no longer works.** The
app serves `frame-ancestors 'none'`, so an in-page iframe has no reachable
`contentDocument`. Narrow widths were checked by constraining `.branch-map` and measuring
`scrollWidth` against `clientWidth` instead, which tests the reflow path that actually
matters.

## What happened on 2026-08-20, part two — the published docs caught up

Everything the project publishes had drifted a full phase behind the code. The README, the
project page (`docs/index.html`) and the engineering guide (`docs/GUIDE.md` + its
hand-written `docs/guide.html`) all described a **linear** story: 151 tests, 37 migrations,
no tree, no takes, no branches. On branch `docs-story-tree`.

**The numbers that were wrong:** 151 → **440** tests, 37 → **64** migrations, "all twelve
phases" → fourteen. Those appear in four places between the README, the project page's stat
tiles, and the guide's results table.

**What was actively misleading, not merely stale.** The guide's §2.2 was *Two coordinate
systems, and the bug class they create*, and it explained the codebase through
`position_of_index`, `note_action_removed` and `settled_story_actions` — **all three deleted
in SP3**. §2.3 explained retry through `Action.variants` and `state_before`. A reader
following either would have gone looking for machinery that isn't there. §2.2 is now *The
story is a tree*, written at the same depth as §1.2 and §1.3: the seven bugs that were all
one bug, the lineage clause and its two properties, why takes group by `parent_id`, cursors
becoming anchors, and what the design is honest about. §2.3 is rewritten around
`state_after` and takes.

**Three screenshots**, shot on a new `backend/tools/shots_fixture.py` — the Bandit Camp demo
scenario driven through eight scripted turns, three discarded takes forked onto branches,
one of them off a branch so the map has to nest. It exists for the same reason
`tree_fixture.py` does: the shots have to be reproducible, and there is still no frontend
test runner. `play-world-state.jpg` was reshot (it predated the whole tree UI);
`branch-map.jpg` and `branches-panel.jpg` are new.

Worth not rediscovering: **`docs/guide.html` is hand-written, not generated from the
Markdown.** Every guide edit is two edits, and the HTML has its own vocabulary
(`.trap`/`.tag` callouts, `.stats`/`.stat`/`.v`/`.k` tiles) that has to be matched by hand.
The two files were checked for tag balance with `html.parser` and both pages were rendered
over a local `http.server` before committing — `file://` URLs are blocked from the browser
tooling, which is worth knowing before trying it again.

---

## What happened on 2026-08-18, part five — the review, and PR #6

The stack went up as **one PR (#6)** rather than seven stacked ones: `sp7-tree-ui` was
sixteen commits ahead of `main` and **zero behind**, so the chain was already linear.
Then a `/code-review high` over `origin/main...origin/sp7-tree-ui` raised nine findings,
answered in commit `00feda8`. **409 tests.** Still not merged, still not deployed.

**The headline finding was rejected, and the reasoning is the part worth keeping.** The
review called it data loss: a memory the player types lands on the head's coordinate, and
a retry withdraws every memory at that coordinate, so the note disappears. Reproduced,
and real. But it is the rule working — *a memory anchored to a node describes that node
and goes when the node goes* — and the product decision is that this is correct.

**The root node is the exception, and the only one.** Migration 62 parks the entire
pre-coordinate bank on depth 0, because 0 is the one depth every branch can see. That
makes the opening node the one place holding memories it never produced, so withdrawing
it would retire a whole bank in a click. `forget_node` now keeps memories with **no
`source_start`** at `lineage.ROOT_DEPTH` and still withdraws a summary that genuinely
ends there — blanket-protecting depth 0 would have rebuilt the dangling-memory problem
`forget_node` replaced `prune_dangling_memories` to prevent. Both directions are tested.

Eight repairs, of which three are worth remembering as classes rather than as bugs:

- **A sibling group breaks every query that assumed one row per depth.** `_latest_narration`
  ordered by `(depth desc, id desc)` and got the *newest* attempt, not the live one, so the
  index screen quoted a take the player had thrown away. Anything ranking actions by
  coordinate needs `live` in the filter.
- **A cap has to count what gets written, not what the file says.** The import counted a v1
  file's turns; each turn expands into a row per saved attempt, so a file inside a
  5,000-action cap could write 50,000 rows. Re-checked after `plan()`, which is pure and
  runs before the adventure row exists.
- **`stateKey`, not `actions.length`, again.** SP7 fixed four components that refreshed on
  story *length* when a branch switch changes *which story*; the attempt-switch path was
  missed by the same reasoning, and `select_variant` restores state, withdraws a memory and
  rewinds both cursors. Still nothing automated can see this: **the frontend has no test
  runner.**

Also: forking a live node on a borrowed ancestor promoted a sibling on a branch the caller
never named (now a 400 naming the branch switch, tested); a v1 import gave a typed memory
no depth, rebuilding the NULL migration 62 exists to remove; a retry after switching back
filed the new attempt into the middle of its group; `rename_branch` answered
`own_actions=0`; and `_backfill_cursor_anchors` numbered every action in the table once per
adventure — a window function the planner cannot push a correlation into, running under
lock at boot against live Postgres. It is correlated to the adventure being updated now,
which makes it an index lookup. **That last one is verified on SQLite only** — no
migration was pointed at Postgres to check it.

**Every new test was checked to fail with its fix reverted** (reverse-apply the app-only
diff, run, restore). A regression test that passes against the unfixed code is not a
regression test, and seven of the eight fail as they should; the eighth — "a summary of
the opening node is still withdrawn" — passes both ways on purpose, because it guards
against over-correcting the root exception rather than against the original bug.

**One behaviour change recorded rather than repaired.** Moving `_history` onto
`context_history.story_actions` also dropped blank-text rows from a user script's
`history` and from `info.actionCount`. It is the right shape — a textless row is this
app's bookkeeping and never reached a prompt — but a script firing "every N actions" now
fires on different turns, and no reading is compatible with both. `plan/14` says so.

## What happened on 2026-08-18, part four — the tree, SP7

The tree reached the screen. A Branches panel beside Plot/Memory/Scripts/Insights lists
every line the story has taken and switches, renames or deletes one; under a retried turn
the ‹ 2/3 › pager is gone, replaced by attempt chips and a **take this path** that forks
when the story has already moved past. **396 tests green**, 15 new in
`test_branch_management.py`. Branch `sp7-tree-ui`, migration 61, no vacuum owed by it.

**Three mockups were built before a line of it was written**, because the spec was one
paragraph and the choice was expensive: a per-node spatial map is a second windowing
problem at 600 nodes. The rail won on the grounds that it does not delay the verification
SP7 exists to do, and the map stays available as a later feature at no extra cost — both
read the same `GET /branches`.

**Two of the three operations SP7 "just needed UI for" did not exist.** `switch` did.
`rename` had no column and no route; `delete` had no route. Migration 61 adds
`branches.name`, and the two endpoints came with it.

**One bug class, in four places, and only a browser could have found it.** The Branches
panel, the Status drawer, Insights and the Memory Bank all refreshed on `actions.length`
— and **a branch switch does not change the length of the story, it changes which story
it is.** So the tree showed one branch while the reader was on a second, Insights showed
the prompt for the path just left, and the scoreboard kept the other line's numbers. The
server was right the whole time and no test could see any of it. All four key on
`${actions.length}:${stateKey}` now.

`backend/tools/branch_fixture.py` was written to catch exactly this and is worth keeping:
a small bootable adventure with real stats whose **two branches have equal path length**,
which is the case a length-based key cannot distinguish. `--keep` could not have found
it. Run it, switch branches, and watch hp go 60 ↔ 95 with the drawer open.

That is now four bugs on this frontend found by exercising it rather than by testing it,
two of them in paths that had just shipped. The pattern is not subtle any more: **this
frontend has no test runner, so anything not driven by hand is unverified.**

**A memory is now attached to a node, always — migration 62.** Hand-written ones used to
carry a NULL depth ("belongs to the adventure, not to a path"), which is a coordinate no
fork can cap, so a note typed on one line followed you onto branches whose story it never
described. They anchor at the head now, and the `unanchored` escape clause in
`lineage.Path.clause` is deleted rather than left to rot.

**And the drawer shows only the path being read**, filtered by the same clause retrieval
uses: the bank you can see is the bank the model can see. Nothing is stranded — a memory
lives on a branch, switching to it shows the memory, and deleting the branch deletes it.
Pinning decides *order*, the path decides *existence*.

Migration 62 lands existing NULL-depth memories at **depth 0 of their branch**, which is
at or before every fork point, so nobody's bank loses a row on deploy. The tip would have
been the tidier-sounding choice and would have emptied them out of every branch forked
earlier than they were typed.

An earlier pass this session shipped the other design — adventure-wide list, `on_path`
flag, *another branch* badge. Anchoring made it redundant and it was removed. Worth
knowing if the phrase turns up in an older commit message.

**The scroll path is finally driven.** 602-action fixture, three prepends of ~16,200 px
each: the same DOM node held viewport top 792 → 787, and the view stayed 48,174 px from
the bottom. PR #2's fix holds. Measuring note worth keeping — the fixture's prose repeats,
so an anchor matched by *text* finds an older copy of the same sentence and reports a
16,000 px jump that never happened. Hold the node.

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
.venv/Scripts/python.exe -m pytest tests/          # 549 tests (~180s)
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

**A real model, without an API key.** `tools/claude_shim.py` serves an OpenAI-compatible
endpoint backed by the local `claude` command line tool. Point Settings at
`http://127.0.0.1:8787/v1`, put any string in the API key field, and pick `sonnet`. Set
the reasoning budget to `0` or `-1`: a positive budget sends `reasoning.max_tokens`,
which Claude 5 models reject with a 400. It costs about $0.04 a turn against the
subscription, and it does not serve embeddings.

```
cd backend
.venv/Scripts/python.exe tools/claude_shim.py      # 127.0.0.1:8787
```

**To exercise the guest path**, which is off in single-user mode:

```
cd backend
AIDND_MULTI_USER=1 AIDND_SECRET_KEY=throwaway AIDND_DB_PATH=$PWD/guest_scratch.db     .venv/Scripts/python.exe -m uvicorn app.main:app --port 8001
curl -c jar.txt http://127.0.0.1:8001/api/auth/me   # mints a guest and its starter
```

Port 8000 is shared with the job-pipeline app, which will squat it and silently shadow
the AI-DnD API — free it before running the backend, or move the vite proxy with
`AIDND_API_PORT`, which is what the `--keep` recipe above does.
