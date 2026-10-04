# Phase 19: YAGNI and duplication sweep

This sweep checks the code against the code-minimalism rules in `CLAUDE.md`.
It looks for code nothing calls and for logic written more than once. Each batch
lands as one commit, and the full check runs after every batch.

## How the sweep found things

- `vulture` at 60% confidence over `backend/app` and `backend/tools`, with route
  handlers filtered out by hand.
- `pylint --enable=duplicate-code` and `jscpd` over the backend, the tests, and
  `frontend/src`.
- Every `request(...)` path in `frontend/src/api.js`, compared against every
  `@router` path in `backend/app/routers`.
- Every frontend `export`, checked for a caller in another file.

## Baseline, 2026-10-02

- 706 backend tests pass in 69 seconds.
- `npm run lint` reports 0 errors and 5 warnings. `npm run build` succeeds.

The sandbox blocks the host tiktoken downloads its encoding from. The suite runs
with `TIKTOKEN_CACHE_DIR` pointed at a local copy of `cl100k_base`, verified
against its published SHA-256. Nothing in the repository changes for this.

## Decisions

| Question | Decision |
|---|---|
| The `/variant` and `/fork` endpoints have no frontend caller | Remove both. Port the tests to fork through `after_id`. |
| `Provider` is an ABC with one implementation | Keep it. A second provider is planned. |
| Test helpers repeated across files | Move the shared helpers into `tests/fakes.py`. Leave each `client` fixture alone. |
| Duplicated setup in the `backend/tools` fixture scripts | Out of scope. Remove only the unused import. |
| Removing `/fork` drops its guard against a take that is live on another branch | Move the guard into `nodes._move_to_after`, which `after_id` uses. |

## Findings and progress

| # | Batch | Finding | Status |
|---|---|---|---|
| 1 | Dead code | `context/cursors.position_of` has no caller | done |
| 1 | Dead code | `tools/memory_ab.py` imports `textwrap` and never uses it | done |
| 1 | Dead code | `api.listVariants` duplicates `api.listTakes` and has no caller | done |
| 1 | Dead code | `countries.js` exports `flagOf` and `countryName`, which only that file uses | done |
| 2 | Router duplication | 15 handlers repeat "load a row, 404 if it is not this adventure's" | done |
| 2 | Router duplication | `undo_turn` and `list_actions` copy the body of `paging.current_window` | done |
| 2 | Dead code | `pyflakes` reports six unused imports in `bundle.py`, two routers, and three tests | done |
| 3 | World state | `apply.py` repeats the cooldown check, the text truncation, and the min and max clamp | done |
| 4 | Streaming | `turns.py` and `chat.py` repeat the reasoning and text stream loop | done |
| 5 | Dead routes | `POST /actions/{id}/variant` and `POST /actions/{id}/fork` have no frontend caller | done |
| 5 | Bug | Playing with `after_id` set to a take that is live on another branch moves that branch's live row, and that branch's story loses the turn | done |
| 6 | Frontend duplication | `splitTags` and the scenario card markup are copied in `Home.jsx` and `Scenarios.jsx` | done |
| 6 | Frontend duplication | The begin-adventure flow (`begin`, `startAdventure`, and the modal) is copied in `Home.jsx` and `Scenarios.jsx` | done |
| 6 | Frontend duplication | `ReasoningBlock` is copied in `Chat.jsx` and `Play/index.jsx` | done |
| 6 | Frontend duplication | The story card handlers are copied in `ScenarioEditor.jsx` and `PlotPanel.jsx` | done |
| 6 | Frontend duplication | `App.jsx` repeats the same `NavLink` class function seven times | done |
| 7 | Test duplication | `_play` is defined in 13 test files, `_retry` in 7, and `_texts`, `_rows`, and `_state` in 4 each | done |

## Batch log

### Batch 1: dead code

Removed `cursors.position_of`, the unused `textwrap` import, and
`api.listVariants`. `flagOf` and `countryName` are no longer exported.

Check: 706 backend tests pass. Lint and build pass.

### Batch 2: router duplication

`deps.get_row_or_404` replaces 15 inline copies of the lookup and
`branches.get_branch_or_404`. `undo_turn` and `list_actions` now return
`current_window`, which takes the optional `before_id` and `limit` that
`list_actions` needs.

`pyflakes` found six unused imports, and this batch removes them. Two reports
remain on purpose. `memorybank` re-exports `story_actions` for
`test_memory_settling.py`. The f-string warning in `tools/stress_session.py` is
in the out-of-scope tools.

Check: 706 backend tests pass.

### Batch 3: world state

`apply.py` now has `_cooldown_rejection`, `_within_limits`, `_truncated`, and
`_not_a_boolean`. `apply_delta`, `apply_override`, `_apply_stat`, and
`_apply_text_stat` call them instead of carrying their own copies.
`_within_limits` keeps the original comparison order, so a `NaN` change still
reports as not clamped.

Check: 706 backend tests pass.

### Batch 4: streaming

`sse.relay` turns a provider's `(kind, chunk)` stream into SSE frames and
collects the text and reasoning. `turns.py` and `chat.py` call it. The two
empty-reply messages stay separate, because each one names the settings
on its own page.

Check: 706 backend tests pass.

### Batch 5: dead routes, and the guard they held

Removed `select_variant`, `fork_from_attempt`, and the `VariantSelect` schema.
The fork logic itself, `nodes.stand_on`, stays, because a turn played with
`after_id` calls it.

The fork endpoint refused a take that is live on another branch. The `after_id`
path did not. In one tab the pager prevents this, because it switches branches
instead of previewing such a take. Two tabs reach it. Tab X previews a spare
take, tab Y forks from that take and switches back, and then tab X writes below
the take. Before the fix, the server returned 200, and the forked branch lost
that turn. A browser run on commit `4c92daa` reproduced it with a fake AI and a
scratch database.
`_move_to_after` now returns the same 400 the endpoint did. The ported test
failed before the fix and passes after it.

How the tests moved:

- `tests/fakes.py` gains `stand_on`, which calls `nodes.stand_on` in a session,
  and `take_id`. Tests that check the state right after a move use them.
- Tests that play a turn right after the move now send `after_id`.
- Two tests are gone with the endpoint, because each checked only a refusal that
  `/variant` made: `test_only_the_newest_turn_can_be_switched` and
  `test_switching_to_a_missing_index_is_rejected`.

Check: 704 backend tests pass, which is 706 minus those two. Lint and build pass.

### Batch 6: frontend duplication

- `components.jsx` gains `splitTags`, `ScenarioCard`, `useBeginAdventure`, and
  `ReasoningBlock`. `Home.jsx`, `Scenarios.jsx`, `Chat.jsx`, and
  `Play/index.jsx` use them. The two card grids still differ in tag count and
  animation delay, so those are props.
- `hooks/useStoryCards.js` holds the add, edit, delete, export, and import
  handlers for story cards. `ScenarioEditor.jsx` and `PlotPanel.jsx` call it.
- `App.jsx` defines `navClass` once.

One behavior changed. On the Scenarios page, a failed create or a failed
scenario fetch now shows an error toast, as it already did on Home. Before,
the rejected promise was dropped and nothing appeared.

Check: lint and build pass. A Playwright run against a scratch SQLite database
covered these flows: card tags and delays on both grids, the Play modal from
Home, the blank adventure from Scenarios, the active nav link, adding a card,
saving an edit after the debounce, exporting, and adding a card from the Play
page's Plot panel. The only console errors were certificate failures on
external font requests through the sandbox proxy.

### Batch 7: test duplication

`tests/fakes.py` gains `play_turn`, `retry_turn`, `story_texts`,
`list_branches`, and `saved_state`. Twelve test modules import them in place
of their own copies. The names avoid `play`, `branches`, and `state` because
tests already use those as local variables.

Some copies differ on purpose and stay local:

- `test_bundle_v2.py` passes an adventure id to every helper.
- The `_rows` copies order their rows differently.
- `test_turn_flow_integration.py` keeps its own `_state`, which returns only
  the script state.

Two copies used a different default turn text. Their calls now pass that text,
so the tests send the same requests as before. The `_play_after` and
`_take_id` helpers from batch 5 are replaced by `play_turn` and `take_id`.

Check: 704 backend tests pass.

## Result

Across the seven batches, the code outside `plan/` lost 1,104 lines and gained
760, for a net 344 fewer lines. The suite went from 706 to 704 tests. The two
removed tests covered only the deleted `/variant` endpoint.

Out of scope:

- The `Provider` ABC stays, per the decision above.
- The `backend/tools` fixture scripts keep their shared setup.
- Each test module keeps its own `client` fixture, because the seed data
  differs.
- The sweep sandbox blocks tiktoken's download host. GitHub Actions can reach
  it, so CI needs no change.
