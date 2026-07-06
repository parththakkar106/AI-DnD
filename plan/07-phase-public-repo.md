# Phase 7 — Public repo & portability

**Goal:** make the repo public-worthy and runnable by anyone on any OS, so the GitHub link is
immediately usable on a resume — before any hosted-deployment work.

## Decisions (confirmed)

| Question | Answer |
|---|---|
| License | **MIT** |
| README media (screenshots/GIF) | **Skip for now** — text-only README; visuals in a later pass |

**Ask before implementing:** GitHub repo name (default suggestion: `ai-dnd`) and whether the
existing local commit history/message is fine to publish as-is.

## Repo hygiene

- [ ] Add `LICENSE` (MIT, current year, Parth Thakkar).
- [ ] Verify no secrets or user data are tracked (`openrouter_key.env`, `data.db` — already
      gitignored and never committed; re-verify before push).
- [ ] Add `backend/.env.example` documenting every env var the app reads (grows in Phase 9).
- [ ] Decide what to do with `CODE_REVIEW_FINDINGS.md` and `plan/` — keep (shows process, good
      for a portfolio) — just give them a one-line mention in the README.

## README rewrite (portfolio-grade, text-only)

- [ ] Pitch paragraph: what it is, what makes it interesting (AI Dungeon-compatible scripting,
      memory bank with embeddings, full prompt transparency/Insights, provider-agnostic).
- [ ] Feature list with pointers into the code (scripting engine, context builder, memory bank).
- [ ] Architecture diagram (reuse/refresh the one in `plan/00-OVERVIEW.md`).
- [ ] Setup instructions for **Windows (start.ps1), macOS/Linux (manual), and Docker**.
- [ ] "Bring your own model" section: Ollama / LM Studio / OpenRouter free models — emphasize it
      runs fully free.
- [ ] Placeholder section for screenshots/GIF (added in a later pass).

## Docker (one-command run for non-Windows users)

- [ ] `Dockerfile`: multi-stage — build frontend (`npm run build`), then Python image serving
      FastAPI with the built SPA mounted (SPA fallback already exists in `app/main.py`).
- [ ] `docker-compose.yml`: single service, volume for `data.db`, port mapping.
- [ ] `start.sh` for macOS/Linux dev parity with `start.ps1` (optional, nice-to-have).
- [ ] Test: `docker compose up` from a clean clone → app works at `http://localhost:8000`.

## Publish

- [ ] Create public GitHub repo (`gh repo create`), push `main`.
- [ ] Add repo description, topics (`ai-dungeon`, `fastapi`, `react`, `llm`, `interactive-fiction`).
- [ ] Confirm the GitHub rendering of README looks right.

## Exit criteria

A stranger on macOS with Docker installed can clone the repo, run one command, open the app,
paste an OpenRouter free-tier key, and play an adventure — without asking you anything.
