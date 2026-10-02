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

## Findings and progress

| # | Batch | Finding | Status |
|---|---|---|---|
| 1 | Dead code | `context/cursors.position_of` has no caller | done |
| 1 | Dead code | `tools/memory_ab.py` imports `textwrap` and never uses it | done |
| 1 | Dead code | `api.listVariants` duplicates `api.listTakes` and has no caller | done |
| 1 | Dead code | `countries.js` exports `flagOf` and `countryName`, which only that file uses | done |
| 2 | Router duplication | 15 handlers repeat "load a row, 404 if it is not this adventure's" | pending |
| 2 | Router duplication | `undo_turn` copies the body of `paging.current_window` | pending |
| 3 | World state | `apply.py` repeats the cooldown check, the text truncation, and the min and max clamp | pending |
| 4 | Streaming | `turns.py` and `chat.py` repeat the reasoning and text stream loop | pending |
| 5 | Dead routes | `POST /actions/{id}/variant` and `POST /actions/{id}/fork` have no frontend caller | pending |
| 6 | Frontend duplication | `splitTags` and the scenario card markup are copied in `Home.jsx` and `Scenarios.jsx` | pending |
| 6 | Frontend duplication | `ReasoningBlock` is copied in `Chat.jsx` and `Play/index.jsx` | pending |
| 6 | Frontend duplication | The story card handlers are copied in `ScenarioEditor.jsx` and `PlotPanel.jsx` | pending |
| 6 | Frontend duplication | `App.jsx` repeats the same `NavLink` class function seven times | pending |
| 7 | Test duplication | `_play` is defined in 13 test files, `_retry` in 7, and `_texts`, `_rows`, and `_state` in 4 each | pending |

## Batch log

### Batch 1: dead code

Removed `cursors.position_of`, the unused `textwrap` import, and
`api.listVariants`. `flagOf` and `countryName` are no longer exported.

Check: 706 backend tests pass. Lint and build pass.
