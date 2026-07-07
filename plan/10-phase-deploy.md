# Phase 10 — Deploy & publish

**Goal:** the app live on Render at a public URL, linked from resume/website alongside the
GitHub repo.

## Decisions (confirmed)

| Question | Answer |
|---|---|
| Platform | **Render** |
| Domain | **Platform URL is fine** (e.g. `ai-dnd.onrender.com`); custom domain can be added later anytime |

**Ask before implementing:** Render tier (free-with-sleep vs ~$7/mo always-on — depends on the
Phase 9 database decision), and the exact service name (it becomes the public URL).

### Decisions (confirmed 2026-07-07)

| Question | Answer |
|---|---|
| Database | **Neon Postgres** (external managed; free tier has no persistent disk). Path fully verified against real Neon — see `plan/09-phase-hardening.md`. |
| Tier | **Free** (sleeps after ~15 min idle; ~30–60s first-wake). |
| Service name | `ai-dnd` (→ `ai-dnd.onrender.com`, adjustable in dashboard). |
| Region | `virginia` (us-east, matches Neon us-east-1). |
| Local Docker preflight | Skipped by choice — Render builds the same Dockerfile in the cloud; its build logs are the image test. |

## Deploy

- [x] `render.yaml` blueprint: web service from the Dockerfile, env vars (SECRET_KEY generated,
      demo-key vars, `MULTI_USER=true`), `/api/health` health check, external Neon Postgres.
      README gained a "Deploy (Render)" section.
- [ ] Set up the Render service, connect the GitHub repo, auto-deploy on push to `main`.
- [ ] Seed production with 2–3 good demo scenarios (public/starter scenarios from Phase 8) so
      first-time visitors have something great to click immediately.
- [ ] Smoke test the live URL: guest play on demo key, register, BYOK flow, scripting, memory
      bank, Insights — from a device/network that isn't yours.
- [ ] Free-tier note: if on free tier, first request after idle takes ~30–60s to wake — add a
      friendly loading state or accept it (revisit tier if it feels bad).

## Post-launch guardrails

- [ ] Watch demo-key spend/usage for the first days (OpenRouter dashboard); confirm caps hold.
- [ ] Set up uptime monitoring (free: UptimeRobot or similar) — optional.
- [ ] Error visibility: Render logs are enough for v1; note how to pull them.

## Resume / website

- [ ] README: add the live-demo link + "Try it" section at the top.
- [ ] 2–3 sentence project blurb for resume/website (stack, the interesting hard parts:
      AI Dungeon-compatible JS scripting sandbox, embedding-based memory bank, prompt
      transparency, guest-first optional auth).
- [ ] Later pass (deferred from Phase 7): screenshots/demo GIF for README and website card.

## Exit criteria

A recruiter clicks one link on your resume, lands on the live app, plays three turns of a demo
scenario as a guest without configuring anything, and can find the GitHub repo from the page.
