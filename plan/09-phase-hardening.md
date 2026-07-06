# Phase 9 — Production hardening

**Goal:** make the app safe and stable to expose to strangers on the internet: config via
environment, resource limits on everything user-controlled, and a single-service production
build.

## Decisions

| Question | Answer |
|---|---|
| Database | **Decide at start of this phase.** SQLite on a persistent disk (zero code change, but Render disks require the ~$7/mo starter tier) vs Postgres (free/cheap managed options, better resume talking point, needs SQLAlchemy URL + migration tweaks). Revisit with current Render pricing. |

**Ask before implementing:** the database choice above, and target monthly budget (drives
Render tier: free tier sleeps after idle + has no persistent disk).

## Configuration

- [ ] All config via env vars with sane local defaults: `DATABASE_URL`, `SECRET_KEY`
      (sessions + API-key encryption), `MULTI_USER`, `CORS_ORIGINS`, demo-key vars (Phase 8),
      port/host. Document each in `backend/.env.example`.
- [ ] Fail fast on missing `SECRET_KEY` when `MULTI_USER=true`.

## Abuse & resource limits

- [ ] **quickjs limits**: per-execution time limit and memory limit on the scripting engine
      (`scripting/engine.py`) — user-submitted JS must not be able to hang or OOM the server.
- [ ] Rate limiting on expensive endpoints (turn generation, script run, auth) — per-user and
      per-IP (e.g. `slowapi`).
- [ ] Request size limits (script source length, memory/story-card text lengths, action text).
- [ ] Cap per-user row counts (adventures, scenarios, scripts, story cards) with friendly errors.
- [ ] Audit debug router (`routers/debug.py`) and `/docs`: admin-only or disabled when
      `MULTI_USER=true` — debug log may contain other users' prompts.

## Production serving

- [ ] Single service: FastAPI serves the built SPA (fallback already exists) — verify the Docker
      image from Phase 7 is production-ready (no `--reload`, multiple workers or async-safe
      single worker; check SQLite + multiple workers interaction before choosing).
- [ ] CORS locked to the deployed origin (moot if same-origin single service — verify).
- [ ] Security headers middleware; cookies `Secure` + `SameSite`.
- [ ] Streaming (SSE) works behind Render's proxy — verify no buffering issues.
- [ ] Structured logging; scrub API keys from all logs and the debug page.

## Database (after decision)

- [ ] If Postgres: swap `DATABASE_URL`, verify JSON-blob columns (embeddings) and
      `migrations.py` work; test full play loop.
- [ ] If SQLite-on-disk: confirm WAL mode + single-worker (or serialized writes) is acceptable.
- [ ] Backup story: platform DB backups (Postgres) or a scheduled dump of the disk (SQLite).

## Exit criteria

Running the production Docker image locally with `MULTI_USER=true`: a hostile user cannot hang
the server with a `while(true)` script, cannot see another user's data or the debug log, gets
rate-limited instead of burning the demo key, and the app streams turns normally the whole time.
