# Code Review Findings — 2026-07-05

Full-codebase review (no git history, so whole project was the scope).
Status: `pending` = not yet fixed, `fixed` = applied, `verify-failed` = finding was wrong on closer look, `skipped` = intentionally not fixed.
Resume point: fix `pending` items top-to-bottom (they are ranked by severity).

## Correctness bugs

### 1. [pending] seed_demo.py doesn't stamp schema version → server crashes on next start
- `backend/seed_demo.py:10`
- Fresh DB created via `Base.metadata.create_all` leaves `PRAGMA user_version` at 0. Next server start sees tables exist, replays every ALTER TABLE migration → `duplicate column name` crash.
- Fix: stamp user_version to latest after create_all (reuse migrations.bootstrap logic).

### 2. [pending] Turn-lock race: two simultaneous turns can run on the same adventure
- `backend/app/routers/adventures.py:302`
- `ensure_not_generating()` runs in the route handler but `_active_turns.add()` only happens when the StreamingResponse generator is first iterated. Double-click Continue → both requests pass the 409 check → duplicate Action.index rows, interleaved generations.
- Fix: atomically test-and-set the lock in the request phase, release in the stream's `finally`.

### 3. [pending] Migration 10 renumbers indexes with a correlated subquery on the table being updated
- `backend/app/migrations.py:34`
- SQLite may evaluate the subquery against partially-updated rows → duplicate indexes survive the "repair".
- Fix: compute new indexes in Python (SELECT ordered, then UPDATE per row).

### 4. [pending] SQLite foreign keys never enabled → CASCADE/SET NULL clauses are dead
- `backend/app/database.py:8`
- Deleting a Script leaves orphaned `scenario_scripts` rows; SQLite rowid reuse can attach a future script to an old scenario.
- Fix: `PRAGMA foreign_keys=ON` via engine connect event.

### 5. [pending] Provider generate() silently yields nothing for non-SSE 200 responses
- `backend/app/providers/openai_compatible.py:84`
- Server that ignores `stream=true` and returns plain JSON → no `data:` lines → empty AI action, no error.
- Fix: buffer non-SSE body and fall back to parsing it as a single JSON completion.

### 6. [pending] Fire-and-forget asyncio task can be GC'd mid-run and wedge the memory bank
- `backend/app/memorybank.py:146`
- `asyncio.create_task` result not referenced; task can vanish silently; adventure ID can stay stuck in `_running`.
- Fix: keep strong refs in a set, discard in done-callback.

### 7. [pending] Memory cursors are list positions but Memory.source_start/end are Action.index values
- `backend/app/memorybank.py:213`
- After any action deletion (indexes keep gaps, positions shift), summarization skips/duplicates blocks and `_update_story_summary` folds the wrong memories.
- Fix: use one space consistently — track cursors by Action.index (position-independent), or renumber on delete.

### 8. [pending] Pinned memories don't count toward memory_top_k cap
- `backend/app/memorybank.py:120`
- 6 pinned + top_k=5 → 11 memories injected, blowing token budget.
- Fix: fill with unpinned only up to `top_k - len(pinned)` (min 0).

### 9. [pending] Embedding-model change → cosine() zips different-dimension vectors silently
- `backend/app/memorybank.py:69`
- Old 768-dim embeddings scored against new 1536-dim query → garbage similarity, no error, never re-embedded.
- Fix: return 0.0 on length mismatch (and ideally clear stale embeddings so _embed_pending redoes them).

### 10. [pending] MAX_STORY_CARDS cap is a no-op for cards created in one hook
- `backend/app/scripting/pipeline.py:59`
- `len(existing) + len(seen_ids) < MAX...` never counts newly added cards (seen_ids ⊂ existing). A script can insert unbounded cards in one turn.
- Fix: count inserts made during the loop.

### 11. [pending] addStoryCard returns 0 (falsy) for the first card, indistinguishable from `false` rejection
- `backend/app/scripting/engine.py:39`
- `if (!addStoryCard(...))` misfires when the card list was empty.
- Fix: return `storyCards.length` (1-based, always truthy) or `true`; document.

### 12. [pending] scenario_id=0 truthiness bug in create_story_card
- `backend/app/routers/story_cards.py:28`
- `scenario_id or ...` picks the wrong owner when id is 0. Use `is not None`.

### 13. [pending] test_connection 500s on non-dict JSON from /models
- `backend/app/routers/settings.py:54`
- Only ValueError caught; `data.get`/`m.get` on non-dict raises AttributeError → 500 instead of `{ok:false}`.
- Fix: catch (ValueError, AttributeError, TypeError) or validate shapes.

### 14. [pending] AI Dungeon exports with `worldInformation` key lose all story cards silently
- `backend/app/routers/scenarios.py:133`
- Import reads only `storyCards`/`worldInfo`; `worldInformation` is in _IGNORED_KEYS so it's dropped and not reported.
- Fix: accept `worldInformation` as a card source too.

### 15. [pending] Shared debounce timer loses edits (Play PlotPanel)
- `frontend/src/pages/Play.jsx:28`
- One `saveTimer` shared by all plot fields AND story-card saves; editing a second thing within 600ms cancels the first pending PATCH → silent data loss.
- Fix: per-key timers (e.g. a Map keyed by field/card id).

### 16. [pending] Same shared-debounce data loss in ScenarioEditor
- `frontend/src/pages/ScenarioEditor.jsx:22`
- Same fix as #15.

### 17. [pending] Continue button silently discards typed input text
- `frontend/src/pages/Play.jsx:469`
- Clicking Continue with text in the box sends type 'continue' (backend ignores text) and clears the input.
- Fix: don't clear input on continue (or treat non-empty input as a normal send).

### 18. [pending] retry() optimistically deletes last AI action with no rollback on failure
- `frontend/src/pages/Play.jsx:475`
- Failed retry (409/network) leaves UI missing an action that still exists server-side.
- Fix: restore the removed action in the catch path (or only remove on first stream event).

### 19. [pending] Settings test()/save() have no error handling → stuck on "Testing…"
- `frontend/src/pages/Settings.jsx:66`
- Rejection leaves `{pending:true}` forever + unhandled rejection.
- Fix: try/catch → setTestResult({ok:false, error:msg}).

### 20. [pending] InsightsPanel race: slow earlier request overwrites newer report
- `frontend/src/pages/Play.jsx:302`
- No staleness guard; slow getAdventureContext can clobber a newer action snapshot.
- Fix: track a request id / cancelled flag in the effect.

### 21. [pending] extractPlaceholders ignores ${...} in story-card trigger keys
- `frontend/src/pages/Scenarios.jsx:50`
- Backend fills placeholders in card.keys but the modal never prompts for those names → literal `${hero}` keys never match.
- Fix: also scan card.keys when collecting placeholder names.

## Cleanup backlog (reuse / simplification / efficiency / altitude — not bugs, apply later)

- **R1** `frontend/src/pages/Play.jsx:26` + `ScenarioEditor.jsx:19` + `ScriptEditor.jsx:34` — three copies of the debounced-autosave + story-card handlers; extract a `useDebouncedSave` hook / shared StoryCardList component. (Fixing bugs #15/#16 properly may accomplish this.)
- **R2** `backend/seed_demo.py:228` — re-implements create_adventure; call the router logic instead.
- **R3** `backend/app/providers/openai_compatible.py:122` — complete() duplicates _request() body building; add a `stream` param to _request().
- **R4** `backend/app/routers/adventures.py:476` — six copies of child-resource get+owner-check+404; extract `get_owned_or_404`.
- **R5** `frontend/src/api.js:26` — streamSSE duplicates request()'s error extraction; extract `throwIfNotOk(resp)`.
- **S1** `backend/app/models.py:210` — `Settings.stream` is dead state (never read); delete column + schema fields.
- **S2** `frontend/src/pages/Play.jsx:6` — MODES and PLAYER_TYPES are identical constants; lastIsAi/canUndo computed twice.
- **E1** `backend/app/context/builder.py:119` — joins+tokenizes the ENTIRE adventure history every turn for the trigger window; walk reversed(actions) until budget instead.
- **E2** `backend/app/scripting/pipeline.py:77` — rebuilds full history dicts + JSON + a blocking commit per script per hook; build once per hook, slice to HISTORY_WINDOW first, commit once.
- **E3** `frontend/src/pages/Play.jsx:561` — every SSE chunk re-renders all action rows; isolate streaming text in a child component / React.memo rows.
- **E4** `backend/app/context/builder.py:48` — Section.tokens uncached, whole context tokenized 2-3×/turn; cache counts, sum sections.
- **E5** `frontend/src/pages/Play.jsx:490` — keydown effect has no dep array → listener re-registered every render.
- **E6** `backend/app/memorybank.py:182` — catch-up summarization awaits blocks sequentially; gather independent blocks.
- **A1** `backend/app/models.py:145` — no UniqueConstraint('adventure_id','index'); index allocation is ad-hoc per writer. (Related to bug #2.)
- **A2** `backend/app/providers/openai_compatible.py:45` — CHAT_CONTINUE_HINT appended below the budgeting layer; assemble prompts in context builder.
- **A3** `backend/app/routers/adventures.py:390` — import endpoints hand-coerce raw dicts; use a Pydantic bundle schema.
- **A4** `backend/app/routers/adventures.py:207` — onModelContext flattens (system, story) and ships everything as user content if modified; pass structure through the hook.
- **A5** `frontend/src/pages/Home.jsx:87` — client appends 'Z' to naive datetimes; emit ISO-8601 with offset from the API instead.
