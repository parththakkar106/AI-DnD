# AI D&D

An AI Dungeon-style interactive storytelling app you can run entirely on your own machine —
with your own AI model. Create scenarios, play open-ended adventures where an LLM narrates the
world, and extend the engine with **JavaScript scripts compatible with real AI Dungeon
scripting**.

Built with FastAPI + SQLite on the backend and React (Vite) on the frontend. Works with **any
OpenAI-compatible endpoint**: Ollama and LM Studio locally, or OpenRouter / OpenAI / Groq / vLLM
in the cloud — endpoint, key, and model are all runtime settings, and OpenRouter's free-tier
models make the whole experience $0.

> 📸 *Screenshots and a demo GIF are coming; for now the fastest tour is running it — one
> command with Docker.*

## Features

- **The full play loop** — Do / Say / Story / Continue actions, streamed AI responses (SSE),
  retry, undo, and edit. Reasoning models supported: "thinking" streams into a collapsible 💭
  panel with its own token budget.
- **AI Dungeon-compatible context engine** — memory, author's note, and story cards (world
  info) triggered by keywords in recent story text, assembled under a token budget
  (`backend/app/context/builder.py`).
- **Insights: total prompt transparency** — every turn stores the exact prompt sent to the
  model; open 🔍 on any AI action to see each context component and why it was included.
- **JavaScript scripting, AI Dungeon-compatible** — `onInput` / `onModelContext` / `onOutput`
  modifiers with shared `state` and a `worldEntries` API, executed in an embedded quickjs
  sandbox (`backend/app/scripting/`). Real AI Dungeon scripts import and run. In-app
  CodeMirror editor included.
- **Auto-summarization + Memory Bank** — the modern AI Dungeon memory system: AI-generated
  memories every few actions, a running story summary, and embedding-based retrieval that
  pulls old-but-relevant facts back into context, with similarity scores visible in Insights
  (`backend/app/memorybank.py`).
- **Import/export** — AI Dungeon-compatible formats for scripts and scenarios; JSON for
  everything.

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
  → assemble context:  [AI instructions] + [plot essentials] + [story summary]
                       + [retrieved memories] + [triggered story cards]
                       + [story history, token-budgeted] + [author's note] + [player action]
  → onModelContext script modifier
  → snapshot context (Insights)
  → provider adapter → AI (streamed)
  → onOutput script modifier
  → store & render
```

## Architecture

```
frontend/   React + Vite SPA  ──HTTP/SSE──►  backend/  FastAPI
                                              ├─ routers/      scenarios, adventures, story cards, scripts, settings, debug
                                              ├─ models.py     SQLAlchemy: Scenario, Adventure, Action, StoryCard, Script, Settings, Memory
                                              ├─ context/      prompt assembly under a token budget
                                              ├─ scripting/    quickjs sandbox + AI Dungeon API surface
                                              ├─ memorybank.py auto-summarization + embedding retrieval
                                              ├─ providers/    OpenAI-compatible adapter, streaming
                                              └─ data.db       SQLite (path overridable via AIDND_DB_PATH)
```

In production the backend serves the built SPA from one port (see `Dockerfile`); in
development Vite proxies `/api` to FastAPI.

## Repo notes

- `plan/` — the phased implementation plan this was built from, kept as a build log
  (phases 1–6 complete; 7–10 cover the public release).
- `backend/.env.example` — the few environment variables the backend reads.
- `CODE_REVIEW_FINDINGS.md` — notes from a self-review pass.

## License

[MIT](LICENSE)
