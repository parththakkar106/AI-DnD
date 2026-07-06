"""Phase 6 — auto summarization + embedding memory bank
(per help.aidungeon.com/faq/the-memory-system).

After each turn, a fire-and-forget task (`run_post_turn`) runs with its own DB
session:
  - every MEMORY_INTERVAL actions (starting at MEMORY_START), each uncovered
    block of actions is summarized into a short "memory";
  - every SUMMARY_INTERVAL actions, the Story Summary is rewritten folding in
    the new memories (the user-edited text is always the base, never clobbered);
  - new memories are embedded (OpenAI-compatible /v1/embeddings) and the bank
    is evicted down to capacity ("forgotten" memories are kept for the UI).

At generation time, `retrieve_memories` embeds the recent story text and ranks
the bank by cosine similarity; the top-K become the "Memories" context section.

All AI calls here are best-effort: failures are logged (debug page) and retried
on a later turn because the cursors only advance on success.
"""

import asyncio
import math

from sqlalchemy.orm import Session

from . import models
from .context import truncate_to_last_tokens
from .database import SessionLocal
from .providers import OpenAICompatibleProvider, ProviderError

MEMORY_INTERVAL = 6  # actions per memory
MEMORY_START = 12  # first memory once the adventure reaches this many actions
SUMMARY_INTERVAL = 15  # actions between Story Summary updates
MAX_MEMORIES_PER_RUN = 5  # cap catch-up work (e.g. imported adventures) per turn
MAX_EMBED_BATCH = 32
RETRIEVAL_WINDOW_TOKENS = 600  # recent story text used as the similarity query
SUMMARY_MAX_WORDS = 250

MEMORY_SYSTEM_PROMPT = (
    "You compress interactive-fiction story excerpts into memories. Respond with "
    "1-2 plain sentences in past tense stating the concrete facts and events "
    "(names, places, items, promises, injuries). No preamble, no commentary."
)
SUMMARY_SYSTEM_PROMPT = (
    "You maintain the running summary of an interactive-fiction story. Respond "
    "with only the updated summary: a single plain-prose overview of the plot "
    f"so far, at most {SUMMARY_MAX_WORDS} words. Preserve important established "
    "facts; compress older events harder than recent ones."
)

# Adventures with a post-turn task currently running (single-process app).
_running: set[int] = set()
# Strong refs to in-flight tasks — the event loop only keeps weak references,
# so a fire-and-forget task can otherwise be garbage-collected mid-run.
_tasks: set[asyncio.Task] = set()


def summary_provider(settings: models.Settings) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        settings.endpoint_url,
        settings.api_key,
        settings.summary_model or settings.model,
        settings.api_mode,
        settings.reasoning_max_tokens,
    )


def embedding_provider(settings: models.Settings) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        settings.endpoint_url, settings.api_key, settings.embedding_model
    )


def cosine(a: list[float], b: list[float]) -> float:
    # Different lengths means the embedding model changed since this vector was
    # stored; zip() would silently score garbage.
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def story_actions(adventure: models.Adventure) -> list[models.Action]:
    return [a for a in adventure.actions if a.text.strip()]


# ---------- Retrieval (runs inside the turn, before build_context) ----------

async def retrieve_memories(
    adventure: models.Adventure,
    settings: models.Settings,
    *,
    update_stats: bool,
) -> dict | None:
    """Returns {"used": [{id, text, similarity, pinned}], "error": str|None},
    or None when the memory bank is off for this adventure. `update_stats`
    bumps use counters (real turns only, not Insights dry runs); the caller's
    commit persists them."""
    if not adventure.memory_bank_enabled:
        return None
    if not settings.embedding_model.strip():
        return {"used": [], "error": "No embedding model configured in Settings."}

    candidates = [m for m in adventure.memories if not m.forgotten and m.embedding]
    if not candidates:
        return {"used": [], "error": None}

    actions = story_actions(adventure)
    query = truncate_to_last_tokens(
        "\n\n".join(a.text for a in actions[-4:]), RETRIEVAL_WINDOW_TOKENS
    )
    if not query.strip():
        return {"used": [], "error": None}

    try:
        [query_vec] = await embedding_provider(settings).embed([query])
    except ProviderError as exc:
        return {"used": [], "error": str(exc)}

    scored = sorted(
        ((cosine(query_vec, m.embedding), m) for m in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    # Pinned memories are always used and count toward top_k, so the injected
    # set never exceeds the configured budget (unless pinned alone exceed it).
    top_k = max(1, settings.memory_top_k)
    used = [(score, m) for score, m in scored if m.pinned]
    remaining = max(0, top_k - len(used))
    used += [(score, m) for score, m in scored if not m.pinned][:remaining]
    used.sort(key=lambda pair: pair[0], reverse=True)

    if update_stats:
        now = models.utcnow()
        for _, m in used:
            m.use_count += 1
            m.last_used_at = now

    return {
        "used": [
            {"id": m.id, "text": m.text, "similarity": round(score, 4), "pinned": m.pinned}
            for score, m in used
        ],
        "error": None,
    }


# ---------- Post-turn background work ----------

def schedule_post_turn(adventure: models.Adventure) -> None:
    """Fire-and-forget summarization/embedding work after a turn is saved."""
    if not (adventure.auto_summarize or adventure.memory_bank_enabled):
        return
    if adventure.id in _running:
        return
    task = asyncio.get_running_loop().create_task(run_post_turn(adventure.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def run_post_turn(adventure_id: int) -> None:
    if adventure_id in _running:
        return
    _running.add(adventure_id)
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, adventure_id)
        settings = db.get(models.Settings, 1)
        if adventure is None or settings is None:
            return
        # Undo/retry can shrink the action list below a stored cursor, which
        # would stall summarization until the story grew past it again.
        count = len(story_actions(adventure))
        adventure.memory_cursor = min(adventure.memory_cursor, count)
        adventure.summary_cursor = min(adventure.summary_cursor, count)
        if adventure.auto_summarize:
            await _create_due_memories(adventure, settings, db)
            await _update_story_summary(adventure, settings, db)
        if adventure.memory_bank_enabled and settings.embedding_model.strip():
            await _embed_pending(adventure, settings, db)
        _evict_over_capacity(adventure, settings, db)
    finally:
        db.close()
        _running.discard(adventure_id)


async def _create_due_memories(
    adventure: models.Adventure, settings: models.Settings, db: Session
) -> None:
    actions = story_actions(adventure)
    provider = summary_provider(settings)
    for _ in range(MAX_MEMORIES_PER_RUN):
        cursor = adventure.memory_cursor
        if len(actions) < MEMORY_START or len(actions) - cursor < MEMORY_INTERVAL:
            return
        block = actions[cursor:cursor + MEMORY_INTERVAL]
        excerpt = truncate_to_last_tokens("\n\n".join(a.text for a in block), 2000)
        try:
            text = await provider.complete(
                MEMORY_SYSTEM_PROMPT, f"Story excerpt:\n\n{excerpt}\n\nMemory:"
            )
        except ProviderError:
            return  # logged in the debug page; cursor unchanged → retried next turn
        if not text:
            return
        db.add(
            models.Memory(
                adventure_id=adventure.id,
                text=text,
                source_start=block[0].index,
                source_end=block[-1].index,
            )
        )
        adventure.memory_cursor = cursor + MEMORY_INTERVAL
        db.commit()


async def _update_story_summary(
    adventure: models.Adventure, settings: models.Settings, db: Session
) -> None:
    actions = story_actions(adventure)
    if len(actions) - adventure.summary_cursor < SUMMARY_INTERVAL:
        return

    # Fold in memories covering the uncovered stretch; fall back to raw story
    # text if memory creation is lagging (e.g. it just failed).
    # summary_cursor is a position into story_actions(); Memory.source_end is
    # an Action.index. Translate the cursor to an index boundary before
    # comparing — the two spaces diverge once actions are deleted or empty.
    if adventure.summary_cursor < len(actions):
        boundary = actions[adventure.summary_cursor].index
    else:
        boundary = actions[-1].index + 1 if actions else 0
    new_events = [
        m.text
        for m in adventure.memories
        if m.source_end is not None and m.source_end >= boundary
    ]
    if new_events:
        events_text = "\n".join(f"- {t}" for t in new_events)
    else:
        block = actions[adventure.summary_cursor:]
        events_text = truncate_to_last_tokens("\n\n".join(a.text for a in block), 2000)

    current = adventure.story_summary.strip()
    user_prompt = (
        f"Current story summary:\n{current or '(none yet)'}\n\n"
        f"New events since the last update:\n{events_text}\n\n"
        "Updated summary:"
    )
    try:
        text = await summary_provider(settings).complete(
            SUMMARY_SYSTEM_PROMPT, user_prompt, max_tokens=600
        )
    except ProviderError:
        return
    if not text:
        return
    adventure.story_summary = text
    adventure.summary_cursor = len(actions)
    db.commit()


async def _embed_pending(
    adventure: models.Adventure, settings: models.Settings, db: Session
) -> None:
    pending = [m for m in adventure.memories if m.embedding is None and not m.forgotten]
    pending = pending[:MAX_EMBED_BATCH]
    if not pending:
        return
    try:
        vectors = await embedding_provider(settings).embed([m.text for m in pending])
    except ProviderError:
        return
    for memory, vector in zip(pending, vectors):
        memory.embedding = vector
    db.commit()


def _evict_over_capacity(
    adventure: models.Adventure, settings: models.Settings, db: Session
) -> None:
    active = [m for m in adventure.memories if not m.forgotten]
    overflow = len(active) - max(1, settings.memory_bank_capacity)
    if overflow <= 0:
        return
    evictable = sorted(
        (m for m in active if not m.pinned),
        key=lambda m: (m.use_count, m.last_used_at or m.created_at),
    )
    for memory in evictable[:overflow]:
        memory.forgotten = True
    db.commit()
