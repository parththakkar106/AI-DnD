"""Phase 6 — auto summarization + embedding memory bank
(per help.aidungeon.com/faq/the-memory-system).

After each turn, a fire-and-forget task (`run_post_turn`) runs with its own DB
session:
  - every MEMORY_INTERVAL actions (starting at MEMORY_START), each uncovered
    block of actions is summarized into a short "memory". Summarization only
    ever reads *settled* actions (see settled_story_actions) — the newest action
    is held back one turn because it is still retryable;
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
from .context import story_actions, truncate_to_last_tokens
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


# BYOK-only by construction: both factories below take the user's own
# endpoint/key straight from Settings and never auth.DEMO_*, so summarization
# and embedding can't spend the shared demo key (their call sites are also
# skipped when using_demo). Don't "fix" this by passing a ProviderConfig in —
# summary_model/embedding_model are free-form user input and are not on the
# demo whitelist.
def summary_provider(settings: models.Settings) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        settings.endpoint_url,
        settings.api_key_plain,
        settings.summary_model or settings.model,
        settings.api_mode,
        settings.reasoning_max_tokens,
    )


def embedding_provider(settings: models.Settings) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        settings.endpoint_url, settings.api_key_plain, settings.embedding_model
    )


def cosine(a: list[float], b: list[float]) -> float:
    # Different lengths means the embedding model changed since this vector was
    # stored; zip() would silently score garbage.
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def settled_story_actions(adventure: models.Adventure) -> list[models.Action]:
    """Story actions old enough to summarize: everything but the newest one.

    Only the *last* action can be retried, so once an action has another action
    after it, its text is final. Summarizing right up to the newest action meant
    a memory could describe an attempt the player then retried away — the
    memory's cursor has already advanced, so it is never regenerated, leaving a
    memory (and, downstream, a story summary) describing narration that is no
    longer in the story. Holding one action back costs a turn of latency and
    makes that unreachable.

    The result is always a prefix of story_actions(), so memory_cursor and
    summary_cursor stay valid positions and no action is ever skipped.
    """
    return story_actions(adventure)[:-1]


def _rewind_cursors_to_index(adventure: models.Adventure, index: int) -> None:
    """Move both cursors back to the position of Action.index `index`.

    The cursors are *positions* into story_actions() while Memory.source_* are
    Action.index values, so the two spaces have to be translated between (they
    diverge as soon as any action is deleted).
    """
    actions = story_actions(adventure)
    position = next((i for i, a in enumerate(actions) if a.index >= index), len(actions))
    adventure.memory_cursor = min(adventure.memory_cursor, position)
    adventure.summary_cursor = min(adventure.summary_cursor, position)


def note_action_removed(adventure: models.Adventure, action: models.Action) -> None:
    """Keep the cursors pointing at the same actions when one is deleted from
    *before* them. Call BEFORE the delete, while the action is still in the list.

    memory_cursor counts actions from the start of the story, so removing an
    earlier action slides every later one down a slot — without this, an action
    that was never summarized shifts into the "already covered" range and is
    skipped forever.
    """
    actions = story_actions(adventure)
    position = next((i for i, a in enumerate(actions) if a.id == action.id), None)
    if position is None:
        return
    if position < adventure.memory_cursor:
        adventure.memory_cursor -= 1
    if position < adventure.summary_cursor:
        adventure.summary_cursor -= 1


def prune_dangling_memories(adventure: models.Adventure, db: Session) -> int:
    """Delete memories that summarized actions which no longer exist (e.g. after
    undo). source_start/source_end are Action.index values; a memory is dangling
    if any covered action is past the current end of the story. Returns the count
    removed.

    Throwing a memory away is not enough on its own: the actions it covered are
    still behind memory_cursor, so they would read as summarized with nothing
    describing them. Rewind to where the earliest discarded memory began, so
    those actions are summarized again.
    """
    max_index = max((a.index for a in adventure.actions), default=-1)
    dangling = [
        m for m in adventure.memories
        if m.source_end is not None and m.source_end > max_index
    ]
    if not dangling:
        return 0
    starts = [m.source_start for m in dangling if m.source_start is not None]
    for m in dangling:
        db.delete(m)
    if starts:
        _rewind_cursors_to_index(adventure, min(starts))
    return len(dangling)


# ---------- Retrieval (runs inside the turn, before build_context) ----------

async def retrieve_memories(
    adventure: models.Adventure,
    settings: models.Settings,
    *,
    update_stats: bool,
    exclude_action_id: int | None = None,
) -> dict | None:
    """Returns {"used": [{id, text, similarity, pinned}], "error": str|None},
    or None when the memory bank is off for this adventure. `update_stats`
    bumps use counters (real turns only, not Insights dry runs).
    `exclude_action_id` drops the action being retried from the similarity
    query, so the discarded attempt can't steer which memories come back."""
    if not adventure.memory_bank_enabled:
        return None
    if not settings.embedding_model.strip():
        return {"used": [], "error": "No embedding model configured in Settings."}

    candidates = [m for m in adventure.memories if not m.forgotten and m.embedding]
    if not candidates:
        return {"used": [], "error": None}

    actions = story_actions(adventure, exclude_action_id)
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
        if adventure is None:
            return
        # Settings are per-user (Phase 8): use the adventure owner's row.
        settings = (
            db.query(models.Settings)
            .filter(models.Settings.user_id == adventure.user_id)
            .first()
        )
        if settings is None:
            return
        # Undo/retry can shrink the action list below a stored cursor, which
        # would stall summarization until the story grew past it again.
        # Deliberately the FULL count, not the settled one: an adventure that
        # was caught up under the old rule can have a cursor equal to the action
        # count, and clamping to settled would rewind it one step, re-covering
        # an already-summarized action in the next block. Both consumers below
        # read settled actions and bail on a negative remainder, so a cursor
        # briefly sitting one past the settled end is harmless.
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
    actions = settled_story_actions(adventure)
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
    actions = settled_story_actions(adventure)
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
