# Phase 2 â€” Play loop + AI provider

**Goal:** the core game is playable. Player acts, AI continues the story, streamed live.

## Provider adapter layer

- [x] `providers/base.py`: abstract `Provider` â€” `generate(prompt_parts, params) -> async stream of text`.
      Takes an assembled context object (system text + story text), so providers decide how to
      map it to their wire format.
- [x] `providers/openai_compatible.py`:
  - Chat mode: system message carries instructions/memory; story history flows in as
    user/assistant text continuation framing suited to a chat endpoint. A configurable
    "narrator" system prompt frames the AI as a second-person storyteller continuing the text.
  - Streaming via SSE from the endpoint, re-streamed to the browser.
  - Optional raw completion mode (`/v1/completions`) for pure-continuation models.
- [x] Errors surfaced cleanly (bad key, connection refused, model not found) with retry affordance.
- [x] "Test connection" on Settings now real.

## Turn engine (`POST /adventures/{id}/actions`)

- [x] Input formatting per AI Dungeon conventions:
  - **Do** â†’ `> You <text>` (normalized to second person, stripped punctuation as needed)
  - **Say** â†’ `> You say "<text>"`
  - **Story** â†’ raw text appended
  - **Continue** â†’ no player text; AI just continues
- [x] Simple context for this phase: opening prompt + full history, truncated from the top to the
      token budget (tiktoken count). Real context engine lands in Phase 3.
- [x] Response streamed to the client via SSE; final text stored as an `ai` action.
- [x] **Retry**: delete last AI action, regenerate with same input.
- [x] **Undo/Erase**: delete last action pair (player + AI) or single action.
- [x] **Edit**: PATCH any action's text in place.

## Play UI

- [x] Story view: continuous prose (not chat bubbles), player actions styled distinctly
      (`>` prefix, accent color), auto-scroll, streaming text renders token-by-token.
- [x] Input bar with mode selector (Do / Say / Story) + Continue button; Enter to send.
- [x] Per-turn controls: Retry, Undo, Edit (inline contenteditable or textarea swap).
- [x] Loading/streaming state, error toast with retry.

## Exit criteria

Point Settings at any OpenAI-compatible endpoint (e.g. Ollama or LM Studio locally), start an
adventure, and play a multi-turn story with all four input modes plus retry/undo/edit, with
streaming output.
