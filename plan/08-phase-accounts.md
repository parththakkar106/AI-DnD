# Phase 8 — Optional accounts & multi-user  ✅ (implemented 2026-07-06, branch `phase-8-accounts`)

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
| Demo key funding | **OpenRouter free models** (owner's key, `:free` whitelist; default `google/gemma-4-26b-a4b-it:free`) |
| Demo turn cap | **20 successful turns/user/day** (failed provider calls don't count) |
| Guest data retention | **Never delete** for v1 (no cleanup job; revisit if the DB grows) |
| Password reset | **Skipped for v1** (no email provider; forgotten password = lost account) |
| Login with an active guest session | Guest is **abandoned**, not merged (its data stays under the guest user) |
| Memory bank on demo key | **Disabled** (no background AI calls on the server-funded key; visible note in the Memory panel/Insights) |

## Data model

- [x] `User` table: id, email (nullable — null means guest), password_hash (nullable),
      created_at, last_seen_at, is_guest flag, demo_turns_used + demo_turns_date.
- [x] `user_id` FK on `Adventure`, `Scenario`, `Script`, `Settings`. Story cards/actions/
      memories inherit scope via their parent (ownership checks resolve the parent).
- [x] Settings **per-user** (row per user_id, unique index). API key **encrypted at rest**
      (Fernet; key derived from `AIDND_SECRET_KEY` or auto-generated `secret.key` next to the
      DB). Key is write-only through the API (`has_api_key` instead of echoing it).
- [x] Migrations 13–23: create "local user" id=1, assign all existing rows to it, unique
      index on settings.user_id; plus a Python bootstrap step that encrypts any plaintext
      api_key (`enc:` prefix marks encrypted values).
- [x] Demo/starter scenarios: `user_id NULL` + `is_public` — everyone sees them read-only;
      `seed_demo.py` seeds the Sunken Crypt scenario as public (its scripts are unowned and
      ship with it; the sample adventure belongs to the local user).

## Auth & sessions

- [x] Guest flow: `GET /api/auth/me` with no/invalid cookie → creates guest User + signed
      long-lived httpOnly cookie (HMAC, `security.py`). Other endpoints 401 without a session;
      the frontend re-establishes via /me and retries once. No signup wall anywhere.
- [x] Register upgrades the guest **in place** (same user_id — data kept). scrypt password
      hashing (stdlib, no extra dep).
- [x] Login switches the session cookie to the account (guest abandoned). Logout clears it.
- [x] Every router handler resolves `current_user`; every query filtered by user_id
      (scenarios/adventures/scripts/story-cards/settings; debug log is local-mode only since
      it's a global buffer).
- [x] Rate limit on register/login: 10 attempts / 5 min per IP (in-memory).
- [x] Local/self-hosted mode stays frictionless: auto-created local user, no login UI unless
      `AIDND_MULTI_USER=1`. Local installs and docker compose behave exactly as before.

## Shared demo key (BYOK fallback)

- [x] Env vars: `AIDND_DEMO_API_KEY`, `AIDND_DEMO_ENDPOINT_URL` (default OpenRouter),
      `AIDND_DEMO_MODELS` (comma whitelist), `AIDND_DEMO_TURNS_PER_DAY` (default 20).
      Demo only activates in multi-user mode.
- [x] No API key configured → demo endpoint/key/whitelisted model; per-user per-day counter;
      429 with a friendly "add your own key in Settings" message when capped (checked before
      the turn starts so no orphaned player action).
- [x] Memory bank + auto-summarization disabled on demo turns (decided: disable, not count).

## Frontend

- [x] Auth UI: Sign up / Log in modal (register default, toggle to login), "Playing as guest —
      sign up to keep your adventures" nudge in the header, account email + logout when
      registered. All hidden in local mode (`multi_user:false` from /me).
- [x] `api.js`: 401 → GET /auth/me (new guest session) → retry once, for both JSON and SSE.
- [x] Settings: demo banner ("Using the shared demo key — N of M free turns left today"),
      write-only API key field with Remove button, debug log hidden in multi-user mode.
- [x] Public scenarios: "demo ✦" badge in the list; read-only editor (fieldset-disabled) with
      an explainer banner; Play/Export still available.

## Exit criteria — verified 2026-07-06

Two sessions (curl cookie jars + Chrome UI): each guest gets an isolated world; register
mid-session keeps all data (same user id); logging in from the second session shows the same
account data; duplicate email → 409; wrong password → 401; rate limiter kicks in. Demo cap
returns 429 at 0 turns left. Migration tested on a copy of the real data.db (rows adopted by
local user, api_key Fernet-encrypted and decrypts back to the original). Live OpenRouter turn
through the encrypted-key path works in local mode. `vite build` + oxlint clean.
