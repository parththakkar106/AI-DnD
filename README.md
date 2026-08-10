# AI D&D

[![CI](https://github.com/parththakkar106/AI-DnD/actions/workflows/ci.yml/badge.svg)](https://github.com/parththakkar106/AI-DnD/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An AI Dungeon-style interactive storytelling app you can run entirely on your own machine —
with your own AI model. Create scenarios, play open-ended adventures where an LLM narrates the
world, and extend the engine with **JavaScript scripts compatible with real AI Dungeon
scripting**.

> ### ▶️ Try it live: **[ai-dnd-1gmp.onrender.com](https://ai-dnd-1gmp.onrender.com)**
> Play a demo scenario as a guest — no sign-up, no API key needed. (Hosted on Render's free
> tier, so the first load after it's been idle takes ~30–60s to wake up.)
>
> Prefer a tour first? The **[project page](https://parththakkar106.github.io/AI-DnD/)** loads instantly.
>
> Want the internals? The **[design notes](https://parththakkar106.github.io/AI-DnD/guide.html)**
> walk through the context budgeting, the world-state referee and the memory bank, and state the
> reasoning behind each one ([Markdown version](docs/GUIDE.md)).

Built with FastAPI + SQLAlchemy on the backend and React (Vite) on the frontend, running on
SQLite locally and Postgres in the cloud. Works with **any OpenAI-compatible endpoint**: Ollama
and LM Studio locally, or OpenRouter / OpenAI / Groq / vLLM in the cloud — endpoint, key, and
model are all runtime settings, and OpenRouter's free-tier models make the whole experience $0.

![The play screen, with the world-state rail open](docs/images/play-world-state.jpg)

*The play screen. The left rail is live world state — the AI proposes changes each turn and a
Python engine decides what actually sticks. The chip under the narration reports what changed.*

## Features

- **The full play loop** — Do / Say / Story / Continue actions, streamed AI responses (SSE),
  retry, undo, and edit. Reasoning models supported: "thinking" streams into a collapsible 💭
  panel with its own token budget.
- **An RPG world-state engine** — a scenario can declare stats, flags, milestones and a named
  cast; the adventure carries their live values. The design is **the AI proposes deltas and a
  Python engine referees them**: it clamps to range, enforces per-turn caps and cooldowns, keeps
  counters monotonic and milestones sticky, then strips the machine-readable block out of the
  prose (`backend/app/worldstate/engine.py`). Word-labelled bands (`40–60: minor damage`) are
  what make the model reliable at it. No dice, no scripting required.
- **AI Dungeon-compatible context engine** — memory, author's note, and story cards (world
  info) triggered by keywords in recent story text, assembled under a token budget
  (`backend/app/context/builder.py`).
- **Insights: total prompt transparency** — every turn stores the exact prompt sent to the
  model; open 🔍 on any AI action to see each context component, its token cost, and why it was
  included.
- **JavaScript scripting, AI Dungeon-compatible** — `onInput` / `onModelContext` / `onOutput`
  modifiers with shared `state` and a `worldEntries` API, executed in an embedded quickjs
  sandbox (`backend/app/scripting/`). Real AI Dungeon scripts import and run. In-app
  CodeMirror editor included.
- **Auto-summarization + Memory Bank** — the modern AI Dungeon memory system: AI-generated
  memories every few actions, a running story summary, and embedding-based retrieval that
  pulls old-but-relevant facts back into context, with similarity scores visible in Insights
  (`backend/app/memorybank.py`).
- **Undo and retry that actually rewind** — undo and retry roll back the world state and script
  state to a per-action snapshot, not just the text, and prune the memories that covered the
  removed turns. Retries are kept as browsable variants (`‹ 2/3 ›`) rather than thrown away.
- **Import/export** — AI Dungeon-compatible formats for scripts and scenarios; JSON for
  everything.
- **Optional accounts for hosted deployments** — by default the app is single-user with zero
  auth friction; set `AIDND_MULTI_USER=1` and visitors play instantly as guests (signed
  session cookie), can register (email + password) at any point to keep their data, and each
  user gets isolated data plus their own encrypted-at-rest API key. A server-funded **shared
  demo key** with a daily turn cap lets people try it without bringing a key
  (`backend/app/auth.py`).

## Screenshots

| | |
|---|---|
| ![Insights panel](docs/images/insights.jpg) | ![Scenario editor](docs/images/scenario-editor-npcs.jpg) |
| **Insights** — the exact prompt for the next turn, broken into components with token counts and the trigger word that pulled each story card in. | **Authoring** — stats with ranges, per-turn caps, cooldowns and word-labelled bands; NPCs the AI addresses by id. |
| ![Script editor](docs/images/script-editor.jpg) | ![Home](docs/images/home.jpg) |
| **Scripting** — the three AI Dungeon hooks with shared persistent `state`, run in a quickjs sandbox. | **Home** — continue a story in progress or start from a scenario. |

## Quick start

### Docker (any OS)

```sh
docker compose up --build
```

Open http://localhost:8000. Your data persists in a named volume across restarts.

### Windows

```powershell
cd backend; python -m venv .venv; .\.venv\Scripts\pip.exe install -r requirements.txt; cd ..
cd frontend; npm install; cd ..
.\start.ps1
```

Open http://localhost:5173 (dev servers; API docs at http://localhost:8000/docs).

### macOS / Linux

```sh
./start.sh   # creates the venv and installs dependencies on first run
```

Open http://localhost:5173.

## Connect a model

Open **Settings** in the app and point it at any OpenAI-compatible endpoint:

| Provider | Endpoint URL | Notes |
|---|---|---|
| Ollama (local) | `http://localhost:11434/v1` | free, private; also serves embedding models for the Memory Bank (e.g. `nomic-embed-text`) |
| LM Studio (local) | `http://localhost:1234/v1` | free, private |
| OpenRouter | `https://openrouter.ai/api/v1` | `:free` models cost nothing (no embeddings on the free tier) |
| OpenAI / Groq / vLLM / … | provider's `/v1` URL | anything speaking `/v1/chat/completions` |

Model name, API key, generation parameters, and (optionally) summary/embedding models for the
Memory Bank are all configured there too — no config files, no rebuild.

## How a turn works

```
player input
  → onInput script modifier
  → assemble context:  [narrator prompt] + [world state + stat guide] + [AI instructions]
                       + [plot essentials] + [story summary] + [retrieved memories]
                       + [triggered story cards] + [story history, token-budgeted]
                       + [author's note] + [player action]
  → onModelContext script modifier
  → snapshot context (Insights)
  → provider adapter → AI (streamed)
  → extract + referee the world-state delta block, strip it from the prose
  → onOutput script modifier
  → store & render
```

## Architecture

```
frontend/   React + Vite SPA  ──HTTP/SSE──►  backend/  FastAPI
                                              ├─ routers/      auth, scenarios, adventures, story cards, scripts, chat, settings, debug
                                              ├─ models.py     SQLAlchemy: User, Scenario, Adventure, Action, StoryCard, Script, Settings, Memory
                                              ├─ migrations.py hand-rolled, versioned via PRAGMA user_version (37 and counting)
                                              ├─ auth.py       guest/registered users, sessions, shared demo key
                                              ├─ security.py   password hashing, cookie signing, API-key encryption
                                              ├─ context/      prompt assembly under a token budget + windowed history queries
                                              ├─ worldstate/   the stat engine: clamps, cooldowns, bands, milestones
                                              ├─ scripting/    quickjs sandbox + AI Dungeon API surface
                                              ├─ memorybank.py auto-summarization + embedding retrieval
                                              ├─ providers/    OpenAI-compatible adapter, streaming
                                              └─ data.db       SQLite (path overridable via AIDND_DB_PATH)
```

In production the backend serves the built SPA from one port (see `Dockerfile`); in
development Vite proxies `/api` to FastAPI.

## Tests

151 backend tests — unit plus full HTTP integration through the real quickjs scripting engine,
with the LLM provider mocked. CI runs them on every push, alongside the frontend lint/build and
a Docker image build.

```sh
cd backend && pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/
```

## Notes on performance

Two of the tests exist because of bugs that were measured rather than guessed at, and they're
the most interesting engineering in the repo:

- **Database egress, cut ~189x.** Every adventure load was pulling `Action.context_snapshot` —
  the entire assembled prompt, ~74 KB per turn — to read two small fields off it. Moving those
  fields into their own columns and marking the heavy ones `deferred` took one adventure load
  from 38.5 MB to 0.20 MB. The backfill runs server-side with dialect-specific SQL so the old
  data never crosses the wire. `tests/test_egress.py` hooks into SQLAlchemy's cursor events and
  fails if a bulk load ever names those columns again.
- **Turn cost, made flat.** Assembling a turn walked the whole story, so it was O(story length)
  — 839 KB of reads at turn 200. `backend/app/context/history.py` now serves tails and slices
  from SQL and measures what it fetched; the same turn costs 129 KB and stops growing at around
  turn 50.

## Deploy (Render)

The repo ships a [`render.yaml`](render.yaml) blueprint: one Docker web service that
serves the SPA and API same-origin, backed by external [Neon](https://neon.tech) Postgres
(the free tier has no persistent disk, so the database lives off-box).

1. Create a **Neon** project and copy its pooled connection string.
2. In Render: **New → Blueprint**, point it at this repo. Render reads `render.yaml`.
3. Fill the secrets it prompts for (`sync: false` vars): `AIDND_DATABASE_URL` (the Neon
   string) and, to offer a no-signup demo, `AIDND_DEMO_API_KEY` / `AIDND_DEMO_MODELS`.
   `AIDND_SECRET_KEY` is generated automatically and kept stable across deploys.
4. Deploy. Pushes to `main` auto-deploy thereafter. Health check: `/api/health`.

On the free tier the service sleeps after ~15 min idle; the first request then takes
~30–60s to wake. Point any keep-warm pinger at `/api/health`, which deliberately doesn't
touch the database — waking the database around the clock costs far more than the cold start
is worth.

## Repo notes

- `plan/` — the phased implementation plan this was built from, kept as a build log. All twelve
  phases are complete; the later files (11, 12) double as design notes for the state-revert and
  world-state work.
- [`docs/GUIDE.md`](docs/GUIDE.md) — design notes: how each subsystem works and why it was built
  that way, with the measurements behind the decisions. Also rendered as a
  [reading page](https://parththakkar106.github.io/AI-DnD/guide.html).
- `backend/.env.example` — the few environment variables the backend reads.
- [`docs/self-review.md`](docs/self-review.md) — a full-codebase self-review pass and what came
  out of it. All correctness findings are resolved.

## License

[MIT](LICENSE)
