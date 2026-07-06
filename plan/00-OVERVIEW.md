# AI D&D — Local AI Dungeon Clone: Plan Overview

A locally hosted web app replicating AI Dungeon's core experience: scenarios, adventures,
AI-driven storytelling, AI Dungeon-style memory/context management, JavaScript scripting
(compatible with real AI Dungeon scripts), and full transparency into what is sent to the AI.

## Confirmed decisions

| Area | Decision |
|---|---|
| Backend | Python — FastAPI + SQLAlchemy + SQLite (single-user, local) |
| Frontend | React SPA (Vite), dark AI Dungeon-like theme |
| AI provider | Provider-agnostic adapter layer; first adapter: **OpenAI-compatible** (`/v1/chat/completions`) — covers Ollama, LM Studio, OpenAI, OpenRouter, vLLM, Groq. Endpoint URL, API key, model name all configurable at runtime. |
| Scripting | **JavaScript, AI Dungeon-compatible** (`onInput` / `onModelContext` / `onOutput` modifiers, shared `state`, `worldEntries` API) via an embedded JS engine (quickjs / py-mini-racer). Real AI Dungeon scripts should import and run. |
| Import/export | AI Dungeon-compatible formats for scripts and scenarios; JSON export/import for everything. |

## Architecture at a glance

```
frontend/   React + Vite SPA  ──HTTP/SSE──►  backend/  FastAPI
                                              ├─ routers/      (scenarios, adventures, actions, scripts, settings, insights)
                                              ├─ models/       (SQLAlchemy: Scenario, Adventure, Action, StoryCard, Script, Settings)
                                              ├─ context/      (prompt assembly: memory, author's note, world info, history budget)
                                              ├─ scripting/    (JS sandbox, AI Dungeon API surface, per-adventure state)
                                              ├─ providers/    (base adapter + openai_compatible.py; streaming)
                                              └─ data.db       (SQLite)
```

## Core domain model

- **Scenario** — template: title, description, opening prompt (with `${placeholders}`), memory,
  author's note, story cards (world info), attached scripts, tags.
- **Adventure** — a playthrough created from a scenario (or blank). Owns its own copy of memory,
  author's note, story cards, script state, and the action list.
- **Action** — one entry in the story: type (`do` / `say` / `story` / `continue` / AI output),
  text, timestamp, plus the **context snapshot** (exact prompt sent to the AI) for Insights.
- **Story Card / World Info** — keys (comma-separated keywords), entry text, optional type/notes.
  Injected into context only when a key matches recent story text.
- **Script** — JS source per hook (input / context / output modifier), attachable to scenarios;
  copied into adventures with persistent `state`.

## The turn pipeline (heart of the app)

```
player input
  → onInput script modifier
  → store player action
  → assemble context:  [AI instructions] + [plot essentials] + [story summary]
                        + [triggered story cards ("World Lore:")] + [story history, token-budgeted]
                        + [author's note inserted N lines from the end] + [player action]
  → onModelContext script modifier
  → snapshot context (Insights)
  → provider adapter → AI (streamed)
  → onOutput script modifier
  → store AI action → render
```

## Phases

1. **[Phase 1 — Foundation](01-phase-foundation.md)**: repo scaffold, FastAPI + SQLite models,
   React shell, scenario/adventure CRUD, settings (endpoint config).
2. **[Phase 2 — Play loop + AI](02-phase-play-loop.md)**: provider adapter with streaming,
   Do/Say/Story/Continue, Retry/Undo/Edit, the adventure play screen.
3. **[Phase 3 — Context engine + Insights](03-phase-context-insights.md)**: memory, author's note,
   story cards with keyword triggering, token budgeting, per-turn prompt snapshots + Insights UI.
4. **[Phase 4 — Scripting](04-phase-scripting.md)**: embedded JS sandbox, AI Dungeon scripting API,
   script editor, script + scenario import/export (AI Dungeon-compatible).
5. **[Phase 5 — Polish](05-phase-polish.md)**: AI Dungeon-like theming pass, placeholders on
   scenario start, adventure export/import, quality-of-life and hardening.
6. **[Phase 6 — Auto Summarization + Memory Bank](06-phase-memory-bank.md)** *(optional)*:
   modern AI Dungeon memory system — AI-generated memories every 6 actions, Story Summary every
   15, embedding-based retrieval of relevant memories into context.

Each phase ends with the app runnable and testable end-to-end.

## Public release (phases 7–10)

Goal: public GitHub repo + hosted multi-user deployment, linkable from resume/website.

| Area | Decision |
|---|---|
| License | MIT |
| Auth | Email + password, **optional** — guest sessions play instantly, register to keep data |
| Signup | Open (rate-limited) |
| LLM keys | BYOK per user + shared server-funded demo key (capped, details TBD in Phase 8) |
| Database (hosted) | **TBD — ask at start of Phase 9** (SQLite-on-disk vs Postgres) |
| Hosting | Render (tier TBD in Phase 10) |
| Domain | Platform URL (custom domain later, optional) |
| README media | Deferred — text-only first, screenshots/GIF in a later pass |

Open questions are recorded at the top of each phase file under "Ask before implementing".

7. **[Phase 7 — Public repo & portability](07-phase-public-repo.md)**: MIT license, portfolio
   README, Dockerfile + compose, cross-platform run instructions, publish to GitHub.
8. **[Phase 8 — Optional accounts & multi-user](08-phase-accounts.md)** *(the big one)*:
   guest-first sessions, optional email+password upgrade, per-user data scoping across all
   routers/tables, per-user encrypted BYOK settings, shared demo key with caps.
9. **[Phase 9 — Production hardening](09-phase-hardening.md)**: env-var config, quickjs
   time/memory limits, rate limiting, size/row caps, locked-down debug surface, production
   serving, database decision.
10. **[Phase 10 — Deploy & publish](10-phase-deploy.md)**: Render blueprint + deploy, seeded
    demo scenarios, live smoke test, resume/website links and blurb.
