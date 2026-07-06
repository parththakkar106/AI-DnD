# Phase 1 — Foundation

**Goal:** runnable skeleton — backend serving a database-backed API, frontend shell with
navigation, scenario & adventure CRUD, and a settings page for the AI endpoint. No AI calls yet.

## Backend

- [x] Project scaffold: `backend/` with FastAPI app, uvicorn entrypoint, `requirements.txt`
      (fastapi, uvicorn, sqlalchemy, pydantic, httpx, tiktoken; quickjs deferred to phase 4).
- [x] SQLite via SQLAlchemy; auto-create `data.db` on first run.
- [x] Models:
  - `Scenario`: id, title, description, prompt, memory, authors_note, tags, created/updated.
  - `StoryCard`: id, owner (scenario or adventure), keys, entry, type, title, description.
  - `Adventure`: id, scenario_id (nullable), title, memory, authors_note, script_state (JSON),
    created/updated.
  - `Action`: id, adventure_id, index, type (`do|say|story|continue|ai|start`), text,
    context_snapshot (JSON, nullable), created.
  - `Script`: id, name, description, input_js, context_js, output_js, library_js.
  - `Settings`: single row — endpoint_url, api_key, model, temperature, max_output_tokens,
    context_token_budget, api_mode (`chat|completion`).
- [x] Routers: CRUD for scenarios, adventures (+ create-from-scenario copying memory/AN/cards),
      story cards, settings. Consistent JSON errors.
- [x] CORS for the Vite dev server; production mode serves built frontend as static files.

## Frontend

- [x] Vite + React scaffold in `frontend/`; router with pages: Home (adventure list),
      Scenarios (list + editor), Play (placeholder), Settings.
- [x] Dark base theme (AI Dungeon-like: near-black background, serif story font, gold/teal accent).
- [x] Scenario editor: title, description, prompt, memory, author's note, story card list editor.
- [x] Settings page: endpoint URL, API key, model name, sampling params; "Test connection" button
      (backend proxies a trivial request — wired for real in Phase 2, stub now).
- [x] "New adventure" flow: pick scenario (or blank) → creates adventure → navigates to Play page.

## Exit criteria

Run `uvicorn` + `npm run dev`, create/edit/delete scenarios with story cards, start an adventure
from one, see it listed on Home, and save endpoint settings — all persisted across restarts.
