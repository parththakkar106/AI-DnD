# Phase 8 — Optional accounts & multi-user

**Goal:** turn the single-user app into a multi-user one where **accounts are optional**:
a visitor can start playing instantly as a guest, and can register (email + password) at any
point to keep their adventures across devices/browsers. This is the largest phase — it touches
every router and most tables.

## Decisions (confirmed)

| Question | Answer |
|---|---|
| Auth method | **Email + password, optional** — guest sessions work without an account |
| Signup policy | **Open signup** (rate-limited) |
| LLM API keys | **BYOK + shared demo key** — users can paste their own key; users without one get limited turns on a server-funded key |

**Ask before implementing:**
- Demo-key specifics: which provider/key funds it, model whitelist (free models only?),
  per-user turn/day cap, what the "out of demo turns" message says.
- Guest data retention: how long before unclaimed guest data is deleted (suggestion: 30 days
  of inactivity).
- Password reset: skip for v1, or implement email-based reset (requires an email provider)?

## Data model

- [ ] `User` table: id, email (nullable — null means guest), password_hash (nullable),
      created_at, last_seen_at, is_guest flag (derivable from email; keep explicit for clarity).
- [ ] Add `user_id` FK to: `Adventure`, `Scenario`, `Script`, `Settings` (and anything else
      global today — audit `models.py`). Story cards/actions inherit scope via their parent.
- [ ] Settings becomes **per-user** (endpoint URL, API key, models, memory-bank config).
      API key **encrypted at rest** (Fernet with a server-side `SECRET_KEY` env var).
- [ ] Migration (in `migrations.py` style): create a "local user", assign all existing rows to
      it — a fresh clone/local install keeps working exactly as before.
- [ ] Demo/starter scenarios: mark as `user_id = NULL` + `is_public` so everyone sees them
      (decide exact mechanism when implementing; seed via `seed_demo.py`).

## Auth & sessions

- [ ] Guest flow: first API call with no session → create guest User, set a signed, long-lived
      httpOnly session cookie. No signup wall anywhere.
- [ ] Register: email + password (hashed with bcrypt/argon2) **upgrades the current guest user
      in place** — same user_id, data automatically kept.
- [ ] Login: standard session issue; logging in from a fresh guest session with existing account
      discards the empty guest (or merges — ask if guest has data).
- [ ] Session middleware/dependency: every router handler resolves `current_user`; **every query
      filtered by `user_id`** (this is the bulk of the diff — go router by router).
- [ ] Rate limits on register/login endpoints (brute-force protection).
- [ ] Local/self-hosted mode stays frictionless: single auto-created local user, no login UI
      unless `MULTI_USER=true` (env var) — resume demo runs multi-user, local clones don't care.

## Shared demo key (BYOK fallback)

- [ ] Server env vars: `DEMO_API_KEY`, `DEMO_ENDPOINT_URL`, `DEMO_MODEL_WHITELIST`,
      `DEMO_TURNS_PER_DAY`.
- [ ] If a user has no API key configured: use demo key, restrict model picker to the whitelist,
      count turns per user per day, friendly error + "add your own key in Settings" when capped.
- [ ] Turn counting includes memory-bank background calls (or disable memory bank on demo key —
      decide when implementing).

## Frontend

- [ ] Auth UI: register/login modal or page, "Save your progress" nudge for guests (subtle,
      e.g. in the header), logout, account menu.
- [ ] `api.js`: send cookies (`credentials: include`), handle 401 → re-establish guest session.
- [ ] Settings page: per-user; show demo-key status ("Using shared demo key — N turns left today").

## Exit criteria

Two different browsers hit the deployed app: each gets its own guest world (adventures invisible
to the other), both can play immediately on the demo key. One registers mid-adventure and its
data survives; logging in from the other browser shows the same account data. Local
`start.ps1` / `docker compose up` still works with zero auth friction.
