# Phase 7 — Public repo & portability

**Goal:** make the repo public-worthy and runnable by anyone on any OS, so the GitHub link is
immediately usable on a resume — before any hosted-deployment work.

## Decisions (confirmed)

| Question | Answer |
|---|---|
| License | **MIT** |
| README media (screenshots/GIF) | **Skip for now** — text-only README; visuals in a later pass |

**Repo name (decided): `AI-DnD`.** Ask before publishing: whether the existing commit
history/messages are fine to publish as-is.

## Repo hygiene

- [x] Add `LICENSE` (MIT, current year, Parth Thakkar).
- [x] Verify no secrets or user data are tracked (`openrouter_key.env`, `data.db` — already
      gitignored and never committed; re-verify before push).
- [x] Add `backend/.env.example` documenting every env var the app reads (grows in Phase 9).
      (Currently just `AIDND_DB_PATH`, added to `database.py` for Docker/hosted volumes.)
- [x] Decide what to do with `CODE_REVIEW_FINDINGS.md` and `plan/` — keep (shows process, good
      for a portfolio) — just give them a one-line mention in the README.

## README rewrite (portfolio-grade, text-only)

- [x] Pitch paragraph: what it is, what makes it interesting (AI Dungeon-compatible scripting,
      memory bank with embeddings, full prompt transparency/Insights, provider-agnostic).
- [x] Feature list with pointers into the code (scripting engine, context builder, memory bank).
- [x] Architecture diagram (reuse/refresh the one in `plan/00-OVERVIEW.md`).
- [x] Setup instructions for **Windows (start.ps1), macOS/Linux (manual), and Docker**.
- [x] "Bring your own model" section: Ollama / LM Studio / OpenRouter free models — emphasize it
      runs fully free.
- [x] Placeholder section for screenshots/GIF (added in a later pass).

## Docker (one-command run for non-Windows users)

- [x] `Dockerfile`: multi-stage — build frontend (`npm run build`), then Python image serving
      FastAPI with the built SPA mounted (SPA fallback already exists in `app/main.py`).
      (3 stages: node build → pip wheel build with gcc for quickjs → slim runtime.)
- [x] `docker-compose.yml`: single service, named volume mounted at `/data`
      (`AIDND_DB_PATH=/data/data.db`), port mapping.
- [x] `start.sh` for macOS/Linux dev parity with `start.ps1` (optional, nice-to-have).
- [ ] Test: `docker compose up` from a clean clone → app works at `http://localhost:8000`.
      **Blocked locally: Docker is not installed on the dev machine.** Verified without Docker:
      production frontend build + backend serving the SPA (deep links OK) + DB-path override
      all work. Test the image on any Docker machine (or let Render build it in Phase 10).

## Publish

- [ ] Create public GitHub repo (`gh repo create`), push `main`.
- [ ] Add repo description, topics (`ai-dungeon`, `fastapi`, `react`, `llm`, `interactive-fiction`).
- [ ] Confirm the GitHub rendering of README looks right.

## Exit criteria

A stranger on macOS with Docker installed can clone the repo, run one command, open the app,
paste an OpenRouter free-tier key, and play an adventure — without asking you anything.
