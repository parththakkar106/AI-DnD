# Phase 6 — Auto Summarization + Memory Bank (optional)

**Goal:** replicate modern AI Dungeon's Memory System (per
help.aidungeon.com/faq/the-memory-system): AI-generated memories, a running Story Summary, and
embedding-based retrieval. This phase makes extra AI calls (summarization + embeddings), so it is
opt-in per adventure and gated on the endpoint supporting it.

## Auto Summarization

- [x] **Memories**: every 6 actions (starting at action 12), summarize that block of
      player actions + AI responses into a short "memory" via a background AI call
      (same provider, cheap/configurable model override).
- [x] **Story Summary**: every 15 actions, update the Story Summary plot component — a running
      overview of the plot — folding in recent memories; compress it when it grows too long.
- [x] Story Summary stays **manually editable**; user edits inform future updates
      (they are the base text for the next summarization pass) but are never overwritten silently.
- [x] Summarization failures are non-fatal: log, retry next interval.
      (Failed calls appear on the debug page; cursors only advance on success, so the
      next turn retries. Implementation: `backend/app/memorybank.py`.)

## Memory Bank

- [x] Store each memory with an **embedding vector** (OpenAI-compatible `/v1/embeddings`;
      embedding model configurable in Settings; feature disabled if unavailable).
- [x] Each turn, embed the recent story text and rank memories by cosine similarity;
      inject the top-K "Used Memories" into context as their own component
      (between Story Summary and story cards in the layout). Pinned memories are always
      included; top-K is a setting (default 5).
- [x] Configurable bank capacity (AI Dungeon tiers: 25–400; ours: a setting, default 200);
      when full, evict least-recently-used/least-retrieved memories ("Forgotten Memories").
- [x] SQLite storage for vectors (JSON blob + in-memory cosine ranking — pure Python,
      no numpy needed at this scale).

## UI

- [x] Memory Bank panel: list memories (used / idle / forgotten), edit or delete, pin favorites,
      see which memories were retrieved for a given turn (🔍 on an AI action → Insights snapshot).
- [x] Insights integration: retrieved memories shown as a context section with similarity scores.
- [x] Adventure settings: toggle auto-summarization / memory bank (per adventure, in the Memory
      panel); summary + embedding models are chosen globally in Settings (deliberate
      simplification — one endpoint config for the whole app).

## Exit criteria

Play a 40+ action adventure: memories appear every 6 actions, the Story Summary updates every 15,
an early-game fact that scrolled out of the raw history gets retrieved via the Memory Bank when it
becomes relevant again, and Insights shows exactly which memories were injected and why.
