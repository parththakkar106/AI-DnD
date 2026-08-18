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
from array import array
from collections import OrderedDict

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, object_session

from . import models, tree, vectors
from .context import cursors, history, lineage, story_actions, truncate_to_last_tokens
from .database import SessionLocal
from .providers import OpenAICompatibleProvider, ProviderError
from .vectors import cosine  # re-exported: the ranking lives here, the maths there

MEMORY_INTERVAL = 6  # actions per memory
MEMORY_START = 12  # first memory once the adventure reaches this many actions
SUMMARY_INTERVAL = 15  # actions between Story Summary updates
MAX_MEMORIES_PER_RUN = 5  # cap catch-up work (e.g. imported adventures) per turn
MAX_EMBED_BATCH = 32
RETRIEVAL_WINDOW_TOKENS = 600  # recent story text used as the similarity query
RETRIEVAL_WINDOW_ACTIONS = 4  # ...taken from this many of the newest actions
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


def set_vector(memory: models.Memory, vector: list[float] | None) -> None:
    """Store (or clear) a memory's embedding.

    Every column that describes the vector moves together: `embedding_blob` is
    what the ranking reads and `embedded` is the flag everything else reads.
    Going through one function is what keeps them in step — and it is also the
    only place a stored vector can change, which is what makes the cache below
    safe to invalidate here and nowhere else.

    The one caller that legitimately cannot come through here is the bulk
    clear in `routers/settings.py` when the embedding model changes. It has to
    set the same two columns by hand; see the note there.
    """
    memory.embedding_blob = None if vector is None else vectors.pack(vector)
    memory.embedded = vector is not None
    cached = _vector_cache.get(memory.adventure_id)
    if cached is not None:
        cached.pop(memory.id, None)


# ---------- The vector cache ----------

# adventure id -> {memory id: vector}, most-recently-used last.
#
# Turns for one adventure arrive back to back, and the bank barely changes
# between them, so re-reading every vector each turn is the same 600 KB over
# and over. Vectors are held as array("f") — 4 bytes a component, the same
# 6 KB the column holds. A list of Python floats would be eight times that.
#
# Correctness rests on two things. Anything that *changes* a vector goes
# through set_vector, which drops that one entry. Anything that *removes* a
# memory from play — eviction, deletion, pruning, an edit clearing the vector —
# takes it out of the catalogue query below, and entries missing from the
# catalogue are dropped on the next read. So nothing has to remember to call an
# invalidate, which is the failure this design is chosen to avoid.
#
# In-process, so it assumes one worker. That is what the deploy runs; a second
# worker would each keep their own copy and both would still be correct on
# eviction and deletion, but a vector rewritten by one could go stale in the
# other until that memory next leaves the catalogue.
_vector_cache: OrderedDict[int, dict[int, array]] = OrderedDict()
VECTOR_CACHE_ADVENTURES = 8  # ~600 KB each at a 100-memory bank


def forget_cached_vectors(adventure_id: int) -> None:
    """Drop an adventure's cached vectors. Only needed when the adventure
    itself goes away — everything else self-corrects (see above)."""
    _vector_cache.pop(adventure_id, None)


def _vectors_for(db: Session, adventure_id: int, ids: list[int]) -> dict[int, array]:
    """The vectors for `ids`, reading only the ones not already held."""
    cached = _vector_cache.get(adventure_id)
    if cached is None:
        cached = _vector_cache[adventure_id] = {}
    _vector_cache.move_to_end(adventure_id)
    while len(_vector_cache) > VECTOR_CACHE_ADVENTURES:
        _vector_cache.popitem(last=False)

    wanted = set(ids)
    for gone in set(cached) - wanted:
        del cached[gone]
    missing = [memory_id for memory_id in ids if memory_id not in cached]
    if missing:
        rows = db.execute(
            select(models.Memory.id, models.Memory.embedding_blob)
            .where(models.Memory.id.in_(missing))
        ).all()
        for memory_id, blob in rows:
            if blob:
                cached[memory_id] = vectors.unpack(blob)
    return cached


def forget_node(db: Session, adventure: models.Adventure, action: models.Action) -> int:
    """Withdraw what a node produced, because the node is being removed.

    Call it before deleting `action` (undo, delete-an-action). A memory hangs
    off the node whose block it ends on, so "which memories described this?" is
    a lookup on `(branch_id, depth)` rather than a scan for rows whose covered
    range has fallen off the end of the story — which is what
    `prune_dangling_memories` did, and it could only ever notice the damage
    after the fact.

    Discarding the memory is half of it. The stretch of story it covered is
    still behind the cursors, so without a rewind those actions read as
    summarized with nothing describing them, silently, for the rest of the
    adventure. `source_start` is where that stretch began; the anchor goes to
    the node before it, which is a depth whether or not anything still sits
    there.

    The opening node is the one exception, because migration 62 parked the
    whole pre-coordinate bank on it — see the comment on `lineage.ROOT_DEPTH`.

    Returns how many memories were withdrawn.
    """
    if action.branch_id is None or action.depth is None:
        return 0  # a pre-tree row: no path contains it, so nothing hangs off it
    doomed = (
        db.query(models.Memory)
        .filter(
            models.Memory.adventure_id == adventure.id,
            models.Memory.branch_id == action.branch_id,
            models.Memory.depth == action.depth,
        )
        .all()
    )
    if action.depth == lineage.ROOT_DEPTH:
        # The opening node is special, and only for memories that describe no
        # stretch of story. Migration 62 parked every memory written before
        # memories had coordinates at depth 0 — that was the choice that took
        # nothing away from anybody, but it also collected them all onto one
        # node, so withdrawing that node would retire a player's whole bank in
        # a single click. A memory with no `source_start` was typed (or
        # migrated), describes nothing that can fall off the end, and so has
        # nothing to be withdrawn *from*: it stays. A summary that genuinely
        # ends here is still withdrawn, because the text it describes is going.
        doomed = [m for m in doomed if m.source_start is not None]
    if not doomed:
        return 0
    starts = [m.source_start for m in doomed if m.source_start is not None]
    for memory in doomed:
        db.delete(memory)
    if starts:
        cursors.rewind_all(adventure, action.branch_id, min(starts) - 1)
    return len(doomed)


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
    db = object_session(adventure)
    if db is None:
        return {"used": [], "error": None}

    # Which memories are in play, and nothing else about them. This used to
    # walk adventure.memories, which loaded every row of the bank *including
    # its vector* — ~31 KB a memory, three megabytes a turn, 96% of everything
    # a turn read. Two ids and a flag per row is about eight bytes.
    #
    # The branch clause is the *whole* lineage here, not the window the story
    # is read through: retrieval is long-range recall, and a memory of what
    # happened forty turns ago is exactly what it exists to find. It stays
    # affordable because memories are sparse — one per six actions — so the
    # ancestry of even a heavily forked story returns tens of tiny rows.
    catalogue = db.execute(
        select(models.Memory.id, models.Memory.pinned).where(
            models.Memory.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Memory),
            models.Memory.forgotten.is_(False),
            models.Memory.embedded.is_(True),
        )
    ).all()
    if not catalogue:
        return {"used": [], "error": None}

    recent = history.tail(adventure, RETRIEVAL_WINDOW_ACTIONS, exclude_action_id)
    query = truncate_to_last_tokens(
        "\n\n".join(a.text for a in recent), RETRIEVAL_WINDOW_TOKENS
    )
    if not query.strip():
        return {"used": [], "error": None}

    try:
        [query_vec] = await embedding_provider(settings).embed([query])
    except ProviderError as exc:
        return {"used": [], "error": str(exc)}

    held = _vectors_for(db, adventure.id, [memory_id for memory_id, _ in catalogue])
    scored = sorted(
        (
            (cosine(query_vec, held[memory_id]), memory_id, pinned)
            for memory_id, pinned in catalogue
            if memory_id in held
        ),
        key=lambda row: row[0],
        reverse=True,
    )
    # Pinned memories are always used and count toward top_k, so the injected
    # set never exceeds the configured budget (unless pinned alone exceed it).
    top_k = max(1, settings.memory_top_k)
    used = [row for row in scored if row[2]]
    remaining = max(0, top_k - len(used))
    used += [row for row in scored if not row[2]][:remaining]
    used.sort(key=lambda row: row[0], reverse=True)
    if not used:
        return {"used": [], "error": None}

    # Only now, for at most top_k rows, is the text worth fetching.
    used_ids = [memory_id for _, memory_id, _ in used]
    texts = dict(
        db.execute(
            select(models.Memory.id, models.Memory.text)
            .where(models.Memory.id.in_(used_ids))
        ).all()
    )

    if update_stats:
        # synchronize_session=False: nothing in this request reads the counters
        # back, and matching the UPDATE against loaded objects would mean having
        # loaded them, which is the cost this whole path exists to avoid.
        db.execute(
            update(models.Memory)
            .where(models.Memory.id.in_(used_ids))
            .values(use_count=models.Memory.use_count + 1, last_used_at=models.utcnow())
            .execution_options(synchronize_session=False)
        )

    return {
        "used": [
            {"id": memory_id, "text": texts.get(memory_id, ""),
             "similarity": round(score, 4), "pinned": pinned}
            for score, memory_id, pinned in used
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
        # No cursor clamp here any more. Undo can leave the story shorter than
        # the mark, and a *position* past the end of the list was a stalled
        # pass until the story grew back past it — hence a clamp on every
        # post-turn run, which had its own trap (clamping to the settled count
        # rewound a caught-up adventure a step and re-covered an action). An
        # anchor past the tip is not a broken value: `settled_after` just
        # reports nothing to do, and the story growing back past it resumes
        # exactly where it left off.
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
    provider = summary_provider(settings)
    for _ in range(MAX_MEMORIES_PER_RUN):
        # Re-read each pass: a memory just committed doesn't change the story,
        # but this loop is the only thing that moves the anchor, so both
        # numbers have to be current.
        anchor = cursors.MEMORY.depth(db, adventure)
        if history.count_after(adventure, anchor) < MEMORY_INTERVAL:
            return  # no full block of story past the mark
        if history.count(adventure) < MEMORY_START:
            return  # ...and the adventure is too short to have started at all
        # (that order on purpose: the common answer is "nothing due", and the
        # first question answers it without asking how long the story is)
        block = history.after(adventure, anchor, MEMORY_INTERVAL)
        if len(block) < MEMORY_INTERVAL:
            return
        excerpt = truncate_to_last_tokens("\n\n".join(a.text for a in block), 2000)
        try:
            text = await provider.complete(
                MEMORY_SYSTEM_PROMPT, f"Story excerpt:\n\n{excerpt}\n\nMemory:"
            )
        except ProviderError:
            return  # logged in the debug page; cursor unchanged → retried next turn
        if not text:
            return
        memory = models.Memory(
            adventure_id=adventure.id,
            text=text,
            source_start=block[0].depth,
            source_end=block[-1].depth,
        )
        # Hang it off the node it summarised, so a fork inherits the memories of
        # the path it forked from and nothing else — and move the mark to that
        # same node. The two are one statement about where this pass has got to,
        # and writing them from the same row is what keeps them in step however
        # gappy the depths underneath are.
        tree.attach_memory(memory, block[-1])
        db.add(memory)
        cursors.MEMORY.anchor_at(adventure, block[-1])
        db.commit()


async def _update_story_summary(
    adventure: models.Adventure, settings: models.Settings, db: Session
) -> None:
    anchor = cursors.SUMMARY.depth(db, adventure)
    uncovered = history.count_after(adventure, anchor)
    if uncovered < SUMMARY_INTERVAL:
        return
    # Where the summary will stand once this run succeeds. Read before the AI
    # call, not after: the mark is the end of the story as this pass saw it,
    # and a turn landing meanwhile must not be quietly claimed as read.
    caught_up = history.newest(adventure)
    if caught_up is None:
        return

    # Fold in the memories of the stretch the summary has not read — every
    # memory hanging off a node past the anchor. Both marks and every memory
    # are now depths on one path, so there is no translation between coordinate
    # systems left to get wrong. Falls back to raw story text if memory
    # creation is lagging (e.g. it just failed).
    new_events = db.execute(
        select(models.Memory.text)
        .where(
            models.Memory.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Memory),
            models.Memory.depth > anchor,
        )
        .order_by(models.Memory.depth)
    ).scalars().all()
    if new_events:
        events_text = "\n".join(f"- {t}" for t in new_events)
    else:
        block = history.after(adventure, anchor, uncovered)
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
    cursors.SUMMARY.anchor_at(adventure, caught_up)
    db.commit()


async def _embed_pending(
    adventure: models.Adventure, settings: models.Settings, db: Session
) -> None:
    # A query, not a walk of adventure.memories: this ran every turn and pulled
    # the whole bank's vectors to find the handful that had none.
    #
    # No branch clause, deliberately, here and in the eviction below. Being
    # embedded is a fact about the row, not about the path being played:
    # skipping a sibling's memories would only mean embedding them later, at
    # the moment somebody switched branches and wanted them ranked. Capacity is
    # the same — the bank belongs to the adventure, and evicting the memories
    # of a story nobody is reading is exactly the right thing to evict first.
    pending = (
        db.query(models.Memory)
        .filter(
            models.Memory.adventure_id == adventure.id,
            models.Memory.embedded.is_(False),
            models.Memory.forgotten.is_(False),
        )
        .order_by(models.Memory.id)
        .limit(MAX_EMBED_BATCH)
        .all()
    )
    if not pending:
        return
    try:
        new = await embedding_provider(settings).embed([m.text for m in pending])
    except ProviderError:
        return
    for memory, vector in zip(pending, new):
        set_vector(memory, vector)
    db.commit()


def _evict_over_capacity(
    adventure: models.Adventure, settings: models.Settings, db: Session
) -> None:
    # Counting and ranking are both things the database does without sending
    # anything back. Walking adventure.memories to count them fetched every
    # vector in the bank, every turn, whether or not anything was over capacity.
    in_this_bank = (models.Memory.adventure_id == adventure.id,
                    models.Memory.forgotten.is_(False))
    active = db.execute(
        select(func.count(models.Memory.id)).where(*in_this_bank)
    ).scalar() or 0
    overflow = active - max(1, settings.memory_bank_capacity)
    if overflow <= 0:
        return
    doomed = db.execute(
        select(models.Memory.id)
        .where(*in_this_bank, models.Memory.pinned.is_(False))
        .order_by(
            models.Memory.use_count,
            func.coalesce(models.Memory.last_used_at, models.Memory.created_at),
        )
        .limit(overflow)
    ).scalars().all()
    if not doomed:
        return  # every active memory is pinned; capacity yields to the pins
    db.execute(
        update(models.Memory)
        .where(models.Memory.id.in_(doomed))
        .values(forgotten=True)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    # The bulk UPDATE went around any loaded objects, so anything still holding
    # the collection would see the evicted memories as active.
    db.expire(adventure, ["memories"])
