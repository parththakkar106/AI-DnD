# Phase 17: refactor for readability

The code works and it is tested. It is also hard to read, because four files hold
most of it, one schema migration is half finished, and the published guide is a
hand-maintained copy of another file. This phase fixes those three things without
changing what the app does.

## How to use this file

This file is both the plan and the running status. Update the progress table at
the end of every stage. Read the table first when you pick the work back up.

Each stage lands as its own commit on `refactor-17`. No stage starts until the
stage before it is green.

## Progress

| Stage | Work | Status | Landed |
|---|---|---|---|
| 0 | Hygiene: worktrees, branches, undocumented settings | done, except the branch deletion | 2026-08-29 |
| 1 | Split the four largest files | done: one test setup, and all four files split | 2026-08-29 |
| 2 | Remove duplication | not started | |
| 3 | SP8: drop the legacy columns | not started | |
| 4 | Documentation | not started | |
| 5 | A frontend test runner | not started | |

## Baseline

Record these numbers before you start. They are how you tell a refactor from a
rewrite.

- 549 backend tests pass in 103 seconds.
- `backend/app` holds 13,486 lines across 45 files.
- `backend/tests` holds 12,221 lines across 38 files.
- `frontend/src` holds 9,171 lines across 21 files.
- The app exposes 70 endpoints and applies 64 migrations on boot.

Run the suite with `cd backend && .venv/Scripts/python.exe -m pytest tests/ -q`.

## What the review found

### The code is clean at the statement level

A scan for unreferenced functions across `backend/app` returned only FastAPI route
handlers, which the decorator references rather than the name. A scan of
`index.css` found 5 unused class names out of 373, and every one of the 5 is either
CodeMirror's or built from a template string. There is no dead code to delete and
no `TODO` or `FIXME` anywhere in the tree.

Read that as a constraint. The gains in this phase come from moving code, not from
finding rot.

### Four files hold most of the complexity

| File | Lines | What it holds |
|---|---|---|
| `backend/app/routers/adventures.py` | 2353 | Nine concerns: the turn engine, branches, takes, import and export, adventure scripts, refresh from scenario, insights, memory CRUD, and action CRUD |
| `frontend/src/pages/Play.jsx` | 2280 | 27 components, plus a 722-line `Play()` holding 18 `useState` calls and 7 `useEffect` calls |
| `frontend/src/index.css` | 2865 | One stylesheet. The `max-width: 720px` block at line 2655 overrides rules written 2000 lines above it |
| `backend/app/worldstate/engine.py` | 918 | Four jobs: parse a delta, apply a delta, render the context sections, and instantiate a schema |

### The schema is half migrated

`plan/14-phase-story-tree.md` defines SP8, which drops the columns the tree
replaced. SP8 was gated on the tree running in production. It now does. Eight
columns are still written on every turn and read by nothing:

- `actions.index`, `variants`, `variant_index`, `variant_count`
- `actions.state_before`, `world_state_before`
- `adventures.memory_cursor`, `summary_cursor`

Until they go, `models.py` carries two vocabularies for one idea, and every reader
has to be told which one is live.

### The published guide is a hand-written copy

`docs/guide.html` is 84 KB of hand-written HTML covering the same material as the
63 KB `docs/GUIDE.md`. Nothing generates one from the other. They have already
drifted: the HTML last changed on 2026-08-20 and the Markdown on 2026-08-28, and
section 3.6, "Counting visits", exists only in the Markdown. GitHub Pages publishes
the HTML, so readers get the stale copy. `docs/architecture.html` is 87 KB of
hand-written HTML with no Markdown source at all.

### Duplication the tests already protect

- `worldstate.apply_delta` and `apply_override` each re-implement the same path
  routing for `flags.`, `milestones.`, and `npc.<id>.`. That is six parallel
  branches at `engine.py:573`, `587`, `617`, `675`, `697`, and `740`.
- 35 of the 38 test files repeat the same 8-line temporary-database prologue. There
  is no `conftest.py`.
- `ScriptedProvider` or `FakeProvider` is defined 12 times, once per test file that
  needs a fake model.
- The debounced autosave helper is copied into `Play.jsx:204`,
  `ScenarioEditor.jsx:23`, and `ScriptEditor.jsx:22`.
- `get_adventure_or_404` is called by hand in about 20 handlers. It is a function,
  not a dependency, so every handler repeats the call.
- `routers/chat.py:22` imports `SSE_HEADERS` and `sse` from `routers/adventures.py`.
  One router reaches into another for shared plumbing.

### One setting is documented nowhere

`AIDND_TRUSTED_PROXY_HOPS` is read at `limits.py:55`. It does not appear in
`backend/.env.example`, `README.md`, or `render.yaml`. It sets how many proxy hops
`client_ip()` trusts, which is what stops a rotated `X-Forwarded-For` header from
buying a fresh rate-limit bucket. If a deployment adds a proxy hop and nobody sets
this variable, the bypass comes back silently.

The unprefixed `DATABASE_URL` that `database.py:24` also accepts turned out to be
documented already, inside the `AIDND_DATABASE_URL` entry in
`backend/.env.example`. No change needed there.

### `Settings.stream` is dead

`models.py:622` defines the column, and `schemas.py:452` and `499` expose it.
Nothing reads it in the backend or the frontend. This is item S1 in
`docs/self-review.md`.

### Local clutter

Everything here is gitignored, so it costs repository weight nothing. It costs disk
and attention.

- Two abandoned worktrees, each a full copy of the tree:
  `.claude/worktrees/simplify-comments` and `.claude/worktrees/sp7-tree-ui`. Both
  branches landed on `main` as squashes.
- 21 local branches. 13 read as unmerged to `git branch --no-merged`, and
  `plan/STATUS.md` already records that they landed as squashes.
- Three stale SQLite files in `backend/`: `data.backup-2026-07-06.db`,
  `data.backup-pre-phase8.db`, and `scroll_fixture.db`, 2.9 MB together. The live
  local database, `backend/data.db`, is not one of them and is not touched.

## Stage 0: hygiene

No source changes. Do this first, so later stages run against a quiet tree.

1. Remove both worktrees with `git worktree remove`, then delete the branches that
   landed as squashes. Verify each one first: a branch is safe to delete when
   `git log main..<branch>` names only commits whose message already appears in
   `git log main`.
2. Delete the three stale `.db` files. Leave `backend/data.db` alone.
3. Add `AIDND_TRUSTED_PROXY_HOPS` to `backend/.env.example`, with the reason it
   exists. Add it to the deploy section of `README.md` too, because that is where
   somebody putting the app behind another proxy looks.

**Check:** the suite still passes, and `git status` is clean.

### What Stage 0 actually did, 2026-08-29

Both worktrees are gone, which freed about 104 MB. Removing `sp7-tree-ui` needed one
extra step: a Vite dev server had been running out of that worktree since
2026-08-18, holding `frontend/.vite` open and owning port 5173. It was serving a tree
54 commits behind `main`. Stopping it freed the directory. Restart the real one with
`start.ps1`.

The three stale `.db` files are deleted. `backend/data.db` is untouched.

`AIDND_TRUSTED_PROXY_HOPS` is now in `backend/.env.example` and in the README deploy
section.

**Still owed.** The 19 squash-landed branches are still there. `git branch -D` is
blocked by the permission classifier, which is a reasonable guard on a destructive
command. All 19 are verified safe by the rule above. Run this to clear them:

```
git branch -D bugfix-code-review docs-story-tree fix-prepend-autoscroll \
  fix-silent-clamps-and-milestone-ids fix-store-refusals-and-delta-wording \
  handover-2026-08-17 measure-post-vacuum-sizes phase-14-story-tree \
  phase-7-public-repo phase-8-accounts sp1-tree-schema sp10-memory-bank-eviction \
  sp2-branch-clause sp3-node-cursors sp4-sibling-nodes sp5-fork-on-continue \
  sp6-bundle-v2 sp7-tree-ui sp7b-take-pager worktree-keep-fixture-and-status \
  worktree-simplify-comments
```

`worktree-keep-fixture-and-status` is the one that needed checking by hand. Its
commit subject appears nowhere in `main`, but the `--keep` flag it adds is on `main`
at `tools/stress_session.py:764`, along with both gotchas the message describes, at
`:686` and `:692`. Only the message differs.

The remote branches are left alone. Deleting those is a separate decision.

## Stage 1: split the four largest files

Every change in this stage moves code. None of it changes behavior. The 549 tests
are the check, and they must pass without being edited, except where this section
says otherwise.

### `routers/adventures.py` becomes a package

Split it into `backend/app/routers/adventures/`:

| Module | Holds | Source lines |
|---|---|---|
| `__init__.py` | The `APIRouter`, the shared dependencies, and re-exports | |
| `paging.py` | `ACTION_LIST_COLUMNS`, `ACTION_PAGE`, `action_window`, `annotate_takes`, `current_window` | 27-146, 1203-1261 |
| `crud.py` | List, create, get, patch, delete, plus the script-state and world-state readers | 225-580 |
| `turns.py` | The turn engine: `generate_turn`, `_generate_turn`, `run_player_turn`, the turn lock, and the SSE helpers | 581-1004 |
| `takes.py` | Retry, variants, takes, forking, and undo | 1005-1192, 1503-1778 |
| `branches.py` | The branch endpoints | 1193-1502 |
| `bundle_io.py` | Export and import | 1779-1837 |
| `scripts.py` | The per-adventure script endpoints | 1838-1941 |
| `refresh.py` | Refresh from scenario | 1942-2152 |
| `insights.py` | The context dry run and the per-action context | 2153-2189 |
| `memories.py` | Memory bank CRUD | 2190-2279 |

Target no file above 450 lines.

**The tests couple to the module, so read this before you start.** Twelve test
files call `monkeypatch.setattr(adventures, "OpenAICompatibleProvider", ...)`.
`monkeypatch` replaces a name in the module where the calling code looks it up, so
re-exporting from `__init__.py` does not keep those patches working. Once
`_generate_turn` lives in `turns.py`, the target becomes
`adventures.turns.OpenAICompatibleProvider`.

Do Stage 2's `conftest.py` work first if you want that to be a one-line change
instead of twelve. Otherwise retarget all twelve here. The other patched names are
`adventures.limits`, `adventures.check_demo_cap`, `adventures.generate_turn`, and
`adventures._active_turns`, each used once.

Keep these importable from the package root, because tests import them by name:
`ACTION_PAGE`, `SNIPPET_MAX`, `_snippet`, `world_delta_of`, `acquire_turn_lock`,
`retry_action`, `undo_turn`, and `_active_turns`.

`_active_turns` is module-level mutable state guarded by a lock. It must live in
exactly one module, `turns.py`, and every other module must import the module and
reach through it. If two modules import the set by value, the lock guards two
different sets and the turn lock stops working.

### What the router split actually did, 2026-08-29

`backend/app/routers/adventures.py` is now a package of 14 modules. The largest
is `turns.py` at 443 lines. Four modules exist that the table above does not
list, because the plan's eleven still mixed unrelated work:

| Extra module | Why it exists |
|---|---|
| `deps.py` | Holds the `APIRouter` and the ownership check. It imports nothing else in the package, so every endpoint module can import the router without importing its siblings. |
| `scenario_text.py` | Copying a scenario's text and cards has two callers, `crud.create_adventure` and `refresh`. Leaving it in either one made the other import an endpoint module. |
| `nodes.py` | Story-tree navigation that four modules use: `last_action`, `next_index`, `next_depth`, `stand_on`, `db_tip`, `delete_turn`. |
| `actions.py` | The three action endpoints. They page and delete rather than play a turn, so they do not belong in `crud.py`. |

Two decisions differ from the plan above.

**The package root does not re-export `acquire_turn_lock` or `_active_turns`.**
The plan said to keep them importable, but that makes a broken patch look like a
working one. Rebinding `adventures.generate_turn` changes the alias and leaves
every caller reading the original, and the test still passes. Leaving those names
off the package root raises `AttributeError` instead. Eighteen test call sites and
four in `backend/tools/` now say `adventures.turns.<name>`. The package root still
re-exports the pure helpers, so `adventures.ACTION_PAGE`, `adventures.undo_turn`,
and `chat.py`'s `from .adventures import SSE_HEADERS, sse` are unchanged.

**`world_delta_of` lives in `turns.py`.** It reads as a world-state helper and sat
beside the world-state endpoints, but `_generate_turn` is its only caller.

`_active_turns` behaved as the plan warned. Every module reaches it as
`turns._active_turns`, and a check confirms the four modules see one set object
and one lock.

One test coupled to the router for an unrelated module: it called
`adventures.worldstate.instantiate`. It imports `app.worldstate` directly now.

The split moved text rather than retyping it. An AST comparison against the
pre-split file confirms all 86 definitions are identical, once the `turns.`
prefix is normalized away. The 549 tests pass, and the OpenAPI schema still lists
the same 35 operations.

### `worldstate/engine.py` becomes a package

Split `backend/app/worldstate/engine.py` into four modules under
`backend/app/worldstate/`:

- `schema.py`: `has_schema`, `instantiate`, `reconcile`, `band_label`, `npc_name`,
  `npc_triggers`, `_initials`.
- `parse.py`: `extract_delta`, `_tolerant_load`, `render_delta_block`,
  `applied_delta`, `refusals`, `render_refusals`.
- `apply.py`: `apply_delta`, `apply_override`, and the shared path resolver Stage 2
  introduces.
- `render.py`: `render_state_section`, `render_reference`, `_stat_line`,
  `_describe_stat`.

`worldstate/__init__.py` re-exports every public name it exports today, so no call
site changes. The engine is imported as a module, not monkeypatched, so this split
carries none of the router's test coupling.

### `pages/Play.jsx` becomes a directory

Split it into `frontend/src/pages/Play/`:

- `index.jsx`: the page component.
- `usePlaySession.js`: the 18 `useState` calls and 7 `useEffect` calls that drive
  one adventure, behind one hook.
- `panels/`: `PlotPanel`, `MemoryPanel`, `ScriptsPanel`, `InsightsPanel`,
  `BranchPanel`.
- `drawers/`: `StatusDrawer`, `WorldStateDrawer`, and the `StatRow`, `StatGroup`,
  `StateTree`, and `StateValue` parts they use.
- `reports/`: `StateChangeChips`, `WorldStateReport`, `ScriptReport`,
  `CacheReport`, `TokenBreakdown`.
- `TakePager.jsx`, `RefreshModal.jsx`.

There is no frontend test runner until Stage 5, so this split is verified by
`npm run lint`, `npm run build`, and by driving the Play screen in a browser. Drive
it. `plan/STATUS.md` records that the last two Play bugs were both found by hand and
were unreachable from any test that existed.

### `index.css` becomes a directory

Split it into `frontend/src/styles/`, imported in order from `index.css`:
`tokens.css`, `base.css`, `nav.css`, `cards.css`, `play.css`, `drawers.css`,
`schema-editor.css`, `insights.css`, `modals.css`, and `theme.css`.

Move each `@media` block next to the rules it overrides, rather than leaving one
`max-width: 720px` block at the end. Keep the source order identical when you move
rules, because CSS resolves ties by order and the file relies on that in at least
two known places, recorded in `plan/STATUS.md` and in the comments at
`index.css:1011` and `1082`.

**Check:** 549 tests pass, `npm run lint` and `npm run build` are clean, and the
Play screen works in a browser at desktop and at 500 px wide.

### What Stage 1 actually did to the frontend, 2026-08-29

`Play.jsx` is now `frontend/src/pages/Play/`, twelve files. The largest is
`index.jsx` at 781 lines. `index.css` is now an `@import` list over
`frontend/src/styles/`, eighteen files.

Two decisions differ from the plan above.

**`usePlaySession.js` does not exist yet.** The page component still owns all of
the session state. Moving eighteen `useState` calls and seven `useEffect` calls
is a rewrite, not a move, and no frontend test would catch a mistake in it
today. It waits for Stage 5.

**The `@media` blocks stayed where they were.** `responsive.css` still holds one
`max-width: 720px` block, at the end of the import order. Moving a `@media` block
next to the rules it overrides moves it earlier in the cascade, which changes
which of two equal-specificity rules wins. Nothing in the test suite would catch
that. Do this after Stage 5.

Each split is verified by a different proof, because neither one has a test:

- CSS: the parts rebuild `index.css` byte for byte, and the built bundle is
  identical before and after at 56686 bytes.
- JSX: every non-blank line of the original appears exactly once, in order, across
  the twelve files. A name-resolution check confirms every identifier each file
  references is defined or imported there, with no unused imports.

The line split stranded a comment at six of the boundaries. A leading comment
sits above the section it describes, so each boundary cut one loose and left it
at the end of the file before it. All six moved to the section they describe.

`npm run lint` and `npm run build` are clean, and 549 tests pass. Driving the
Play screen covered the story view, all five panels, both drawers including the
world-state edit form, the branch map, the refresh dialog, and the take pager,
which stepped onto a take that lives on another branch and switched to it. The
console reported no errors. The extension cannot resize the render viewport and
the app sends `X-Frame-Options: DENY`, so the narrow-width check ran by setting
the `max-width` media queries to `all` in the live stylesheet. All 71 narrow
rules found their elements: the nav collapses to one button, the panel tabs move
onto the title row, a panel fills the screen, the composer stacks, and both
drawers become edge tabs.

## Stage 2: remove duplication

Each item here is small and is covered by tests that already exist.

1. **One path resolver in `worldstate`.** `apply_delta` and `apply_override` route
   `flags.<name>`, `milestones.<id>`, `npc.<id>.<stat>`, `world.<stat>`, and
   `player.<stat>` with parallel code. Extract a resolver that returns the
   container, the stat definition, and the kind, then let the two functions differ
   only in the write rule. Their rules genuinely differ, so do not merge the
   functions themselves. `apply_override` ignores `cooldown`,
   `max_delta_per_turn`, and the rule that a counter cannot decrease, and it lets a
   milestone be un-set. `tests/test_worldstate.py` covers both.
2. **Add `backend/tests/conftest.py`.** Move the temporary-database prologue there,
   so the other 35 files drop 8 lines each. The prologue has to run before
   `from app.main import app`, because `main.py` calls `bootstrap(engine)` at import
   time. A `conftest.py` runs before any test module, which satisfies that.
3. **One fake provider.** Move `ScriptedProvider` and `FakeProvider` to `conftest.py`
   as one fixture, replacing 12 copies.
4. **Move the SSE helpers out of the router.** Put `sse`, `SSE_HEADERS`, and
   `turn_error` in `backend/app/sse.py`, so `chat.py` stops importing from
   `adventures`.
5. **Make `get_adventure_or_404` a dependency.** About 20 handlers repeat the call.
   A `Depends` removes the line from each one and puts the ownership check in the
   signature, where a reader looking for it expects it.
6. **One `useDebouncedSave` hook** in `frontend/src/hooks/`, replacing the three
   copies.
7. **Delete `Settings.stream`.** Remove the column, the two schema fields, and add
   the migration that drops it. This is item S1 in `docs/self-review.md`. Mark it
   applied there.

**Check:** 549 tests pass. Test count may drop if consolidating fixtures removes a
duplicate case. If it does, say which case and why in the commit message.

## Stage 3: SP8, drop the legacy columns

This is the only stage that touches the production database. Follow
`plan/14-phase-story-tree.md`, which specifies it.

1. Confirm nothing reads the eight columns. `ACTION_LIST_COLUMNS` in the adventures
   package lists `index`, `variant_count`, and `variant_index` today, so that tuple
   changes here. `bundle.py:37` and `context/history.py:424` both describe `index`
   as unread; verify that rather than trusting the comment.
2. Add the migration that drops them. Remove the columns from `models.py`, and
   remove the fields from `schemas.py` and from `ActionOut`.
3. Deploy, then run `VACUUM FULL actions;` on the direct Neon endpoint, not the
   `-pooler` one. Dropping a column rewrites toasted values, and nothing reclaims
   that space on its own. `plan/STATUS.md` records what happened the last time
   nobody ran it: the database reached 144.2 MB against a 512 MB tier.
4. `VACUUM FULL` takes an `ACCESS EXCLUSIVE` lock, so the app blocks on `actions`
   for the duration. It took 5.5 seconds at 144 MB.

**Check:** 549 tests pass, `/api/health` answers `{"ok":true}` after the deploy, and
one existing adventure opens, takes a turn, retries it, and pages back through the
transcript.

## Stage 4: documentation

1. **Generate `docs/guide.html` from `docs/GUIDE.md`.** Write a small build script
   that renders the Markdown into the existing hand-written HTML shell, keeping the
   current styles and metadata. After that, one edit updates both. Note in
   `README.md` that the HTML is generated and that you edit the Markdown.
2. **Decide what `docs/architecture.html` is.** It has no Markdown source. Either
   give it one and generate it the same way, or state at the top of the file that
   it is hand-written, so the next person does not look for a source that does not
   exist.
3. **Split `plan/STATUS.md`.** It is 63 KB, and most of it is dated session logs.
   Keep "Pick up here", "Things worth remembering", and "Running things" in
   `STATUS.md`. Move the dated entries to `plan/history/`, newest file first.
4. **Add `docs/DEVELOPING.md`.** Record how to run the app, how to run the tests,
   where each subsystem lives, and the invariants a newcomer breaks first:
   - Anything that reads `adventure.actions` during generation takes
     `exclude_action_id`. It leaks into four places, not one.
   - `memory_cursor` and `summary_cursor` are positions into `story_actions()`, and
     `Memory.source_start` and `source_end` are `Action.index` values. The two
     spaces diverge as soon as anything is deleted.
   - `/auth/me` must not raise. It is the SPA's bootstrap call, so anything it
     touches that can raise takes the whole frontend down.
   - The global field rule in the stylesheet keys off `input[type=...]`, so a bare
     `<input>` and any new input type get browser default styling until you add
     them.
   - A modal opened from `.side-panel` needs `createPortal`, because the panel's
     filling transform animation makes it the containing block for
     `position: fixed`.

**Check:** the generated HTML matches the Markdown section for section, including
section 3.6. Every link in `README.md` and `docs/index.html` resolves.

## Stage 5: a frontend test runner

`plan/STATUS.md` names the missing runner as the reason this project keeps finding
UI bugs by hand. Two shipped bugs were unreachable from any test that existed.

1. Add Vitest, React Testing Library, and jsdom. Add a `test` script to
   `frontend/package.json`.
2. Add the step to the existing `frontend` job in `.github/workflows/ci.yml`,
   between lint and build.
3. Write the first tests against the two bug classes that already recurred:
   - The take pager renders when a retry's reply arrives over SSE. The stream builds
     its own `ActionOut`, so it was the one payload that never carried the
     annotation.
   - Retaking a player turn does not produce `> You > You`. The editor is seeded
     with stored text that is already formatted.
4. Add a test for `usePlaySession` from Stage 1, since that hook now holds the state
   the page used to hold inline.

**Check:** `npm test` passes locally and in CI.

## Risks

| Risk | Where | How you catch it |
|---|---|---|
| A monkeypatch silently stops patching, and a test passes while calling a real provider | Stage 1, `routers/adventures` split | After retargeting, break the fake on purpose and confirm the tests that use it fail |
| `_active_turns` ends up imported by value into two modules, so the turn lock guards two sets | Stage 1 | `tests/test_state_revert.py` reaches for `adventures._active_turns`. Keep that import path working, and confirm a second concurrent turn still returns 409 |
| A CSS rule changes meaning because the split reorders it | Stage 1, stylesheet split | Compare the built CSS before and after. Order within each section must not change |
| The column drop rewrites the table and nobody reclaims the space | Stage 3 | Run `VACUUM FULL actions;` on the direct endpoint, and read sizes from `sum(octet_length(col))` rather than `n_live_tup` |
| Splitting `Play.jsx` breaks something no test covers | Stage 1 | Drive the Play screen by hand at both widths. Stage 5 exists to shrink this risk for next time |

## What this phase does not do

- It does not replace the hand-rolled migration runner with Alembic. 64 migrations
  run correctly, and `docs/GUIDE.md` section 2.6 already explains the choice.
- It does not move `bootstrap(engine)` out of import time in `main.py`. The current
  behavior means a failed migration means no service, which is deliberate. Stage 2's
  `conftest.py` removes the boilerplate that import-time bootstrapping forces on
  tests, which is the part that actually hurts.
- It does not change any API shape, any database content, or any prompt.
