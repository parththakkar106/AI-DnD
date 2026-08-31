"""Phase 6: automatic summarization and the embedding memory bank.

This follows AI Dungeon's memory system. See
help.aidungeon.com/faq/the-memory-system.

After each turn, `run_post_turn` runs as a fire-and-forget task with its own
database session. It does three things:

- Every `MEMORY_INTERVAL` actions, starting once the adventure reaches
  `MEMORY_START` actions, it summarizes each uncovered block of actions into a
  short memory.
- Every `SUMMARY_INTERVAL` actions, it rewrites the story summary to include the
  new memories. The rewrite always starts from the text the user edited and
  never discards it.
- It embeds new memories through an OpenAI-compatible `/v1/embeddings` endpoint,
  then evicts the bank down to its capacity. Evicted memories are marked as
  forgotten and kept so that the UI can still show them.

When the app generates a turn, `retrieve_memories` embeds the recent story text
and ranks the bank by cosine similarity. The highest-ranked memories become the
Memories section of the context.

Every AI call in this module is best-effort. A failure is logged to the debug
page and retried on a later turn, because the cursors advance only after a call
succeeds.
"""

import asyncio
from array import array
from collections import OrderedDict

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, object_session

from . import models, tree, vectors
from .context import (
    cursors,
    history,
    lineage,
    match_cards,
    story_actions,
    truncate_to_last_tokens,
)
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

# ---- The cast brief (Phase 18b) ----
# How many characters the brief names, and how much of each description it
# carries. The cast is authored content rather than generated, so it is small in
# practice; these are ceilings against a scenario with a very large cast, not a
# budget anyone is expected to reach.
MAX_CAST_MEMBERS = 8
CAST_ENTRY_CHARS = 240
SETTING_TOKENS = 300  # of `adventure.memory`, the plot essentials

# A ceiling on one memory, in words. Measured, not guessed: with only
# "1-2 plain sentences" to go on, a real model wrote 34 words for one block and
# 105 for the next, and a 105-word memory is a paragraph. Five of those are
# injected per turn at the default `memory_top_k`, so the bank's cost is set
# here. A stated number also holds the length steady between memories, which is
# the same consistency the framing rule buys for the wording.
MEMORY_MAX_WORDS = 50

# The framing rule is the larger half of this prompt, and it is worth the
# tokens. Without it the model chooses a person per call, so one bank ends up
# holding "You entered the crypt", "The player entered the crypt" and "He
# entered the crypt" for the same kind of event.
#
# The reason is stated, not just the rule. A memory is retrieved in isolation
# months of story later, and a model told why bare pronouns fail complies far
# more consistently than one handed a bare instruction.
#
# The protagonist's name lives in the user message, in the Cast, rather than
# here. That keeps this prompt constant across every adventure and every call.
MEMORY_SYSTEM_PROMPT = (
    "You compress interactive-fiction story excerpts into memories. Respond with "
    "1-2 plain sentences in past tense stating the concrete facts and events "
    f"(names, places, items, promises, injuries), in at most {MEMORY_MAX_WORDS} "
    "words. Keep the details a later scene could turn on — a name, a promise, an "
    "injury, where something is — and drop the ones it could not.\n\n"
    "Write in the third person. The excerpt is written in the second person: "
    '"you" is the protagonist, who is named in the Cast. Refer to the '
    'protagonist by that name, never as "you". If the Cast gives no name for '
    'them, call them "the player". Name the other characters too rather than '
    'writing "he", "she" or "they" on their own — this memory will be read on '
    "its own, much later, with nothing around it to say who a pronoun meant.\n\n"
    "No preamble, no commentary."
)
SUMMARY_SYSTEM_PROMPT = (
    "You maintain the running summary of an interactive-fiction story. Respond "
    "with only the updated summary: a single plain-prose overview of the plot "
    f"so far, at most {SUMMARY_MAX_WORDS} words. Preserve important established "
    "facts; compress older events harder than recent ones.\n\n"
    "Write in the third person, and name the characters. Refer to the "
    'protagonist by the name given in the Cast, or as "the player" if the Cast '
    'gives no name. Never address them as "you".'
)

# Adventures with a post-turn task currently running (single-process app).
_running: set[int] = set()
# Strong references to tasks that are still running. The event loop holds only
# weak references, so without this set a fire-and-forget task can be garbage
# collected before it finishes.
_tasks: set[asyncio.Task] = set()


# Both factories below use the user's own key by construction. They read the
# endpoint and key from `Settings` and never from `auth.DEMO_*`, so
# summarization and embedding cannot spend the shared demo key. Their call sites
# are also skipped when `using_demo` is true.
#
# Do not change these to accept a `ProviderConfig`. `summary_model` and
# `embedding_model` are free-form user input and are not on the demo allowlist.
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

    This function updates both columns that describe the vector. The ranking
    reads `embedding_blob`, and everything else reads the `embedded` flag.
    Routing every write through one function keeps the two in step, and it makes
    this the only place a stored vector changes. That is why the cache below can
    be invalidated here and nowhere else.

    One caller cannot use this function: the bulk clear in
    `routers/settings.py` that runs when the embedding model changes. It sets
    the same two columns directly. See the comment there.
    """
    memory.embedding_blob = None if vector is None else vectors.pack(vector)
    memory.embedded = vector is not None
    cached = _vector_cache.get(memory.adventure_id)
    if cached is not None:
        cached.pop(memory.id, None)


# ---------- The vector cache ----------

# Maps an adventure id to a dict of memory id to vector, with the
# most recently used adventure last.
#
# Turns for one adventure arrive one after another, and the bank changes little
# between them. Reading every vector on each turn fetches the same 600 KB
# repeatedly. This cache stores vectors as `array("f")`, which uses 4 bytes per
# component and matches the 6 KB the column holds. A list of Python floats would
# use eight times as much.
#
# Two rules keep the cache correct. Any code that changes a vector calls
# `set_vector`, which removes that entry. Any code that removes a memory from
# play removes it from the catalogue query below, and the next read discards
# entries that the catalogue no longer lists. Eviction, deletion, pruning, and
# an edit that clears the vector all work this way. No code path has to remember
# to invalidate the cache, which is the error this design avoids.
#
# The cache lives in the process, so it assumes one worker, which is what the
# deploy runs. With two workers, each keeps its own copy. Both stay correct
# about eviction and deletion, but a vector rewritten by one worker can remain
# stale in the other until that memory leaves the catalogue.
_vector_cache: OrderedDict[int, dict[int, array]] = OrderedDict()
VECTOR_CACHE_ADVENTURES = 8  # ~600 KB each at a 100-memory bank


def forget_cached_vectors(adventure_id: int) -> None:
    """Drops an adventure's cached vectors.

    Call this only when the adventure itself is deleted. Every other case
    corrects itself, as described in the comment above.
    """
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

    Call this before deleting `action`, which undo and the delete-action
    endpoint both do. A memory attaches to the node its block ends on, so
    finding the memories that describe a node is a lookup on `(branch_id,
    depth)`. The earlier `prune_dangling_memories` instead scanned for rows
    whose covered range no longer existed, so it could only detect the problem
    after it occurred.

    Deleting the memory is half the work. The stretch of story it covered still
    sits behind the cursors. Without a rewind, those actions count as
    summarized while nothing describes them, and nothing reports the problem for
    the rest of the adventure. `source_start` records where that stretch began,
    so the anchor moves to the node before it. That depth is valid whether or
    not a node still occupies it.

    The opening node is the one exception, because migration 62 placed the whole
    pre-coordinate bank on it. See the comment on `lineage.ROOT_DEPTH`.

    Returns the number of memories withdrawn.
    """
    if action.branch_id is None or action.depth is None:
        return 0  # A pre-tree row. No path contains it, so nothing refers to it.
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
        # Special case for the opening node, and only for memories that
        # describe no stretch of story. Migration 62 placed every memory written
        # before memories had coordinates at depth 0. That choice preserved
        # every memory, but it also placed them all on one node, so withdrawing
        # that node would delete a player's entire bank in one action.
        #
        # A memory with no `source_start` was either typed by the player or
        # migrated. It describes no actions, so no deletion can invalidate it,
        # and it stays. A memory that genuinely summarizes a block ending here is
        # still withdrawn, because the text it describes is being deleted.
        doomed = [m for m in doomed if m.source_start is not None]
    if not doomed:
        return 0
    starts = [m.source_start for m in doomed if m.source_start is not None]
    for memory in doomed:
        db.delete(memory)
    if starts:
        cursors.rewind_all(adventure, action.branch_id, min(starts) - 1)
    return len(doomed)


# ---------- The cast brief ----------

def _cast_line(name: str, entry: str, *, protagonist: bool = False) -> str:
    """One roster line: who they are, and nothing about where they stand now."""
    who = f"{name} — the protagonist" if protagonist else f"{name} —"
    entry = " ".join(entry.split())  # collapse newlines: this is a one-line roster
    if len(entry) > CAST_ENTRY_CHARS:
        entry = entry[:CAST_ENTRY_CHARS].rsplit(" ", 1)[0] + "…"
    if protagonist:
        return f"- {who}." if not entry else f"- {who}. {entry}"
    return f"- {name} — {entry}" if entry else f"- {name}"


def cast_brief(adventure: models.Adventure, text: str) -> str:
    """Returns who appears in `text`, and what the story is about.

    This is the context the summarizer never had. It was handed six actions of
    second-person prose and nothing else, so the only honest memory it could
    write for `You push the door open. She grabs your arm.` was "You entered a
    room and she stopped you." — which, retrieved forty turns later, names
    nobody.

    Three rules hold this together.

    **Fixed descriptions only, never live values.** It is tempting to add
    `Gwen: trust 40 (wary)`. That would make the same event summarized at two
    different times come out framed differently, which is the fault this whole
    change exists to remove.

    **The cast comes from the story cards, not from `stat_schema`.** Every NPC a
    scenario defines is already turned into a story card at adventure creation
    (`scenario_text.scenario_card_specs`), deduplicated against the hand-written
    ones by name. Reading the cards therefore covers the schema NPCs, the
    author's own cards, and an adventure with no RPG layer at all, through one
    path instead of three.

    **Keyword matching alone is not enough here, which is why the roster is
    topped up.** The turn prompt includes a card only when its trigger words
    appear, and that is right for lore: a card nobody mentioned is not relevant
    to the next sentence. It is wrong for this brief. The block that most needs
    a cast is exactly the one written in bare pronouns — "she grabs your arm"
    matches no keyword, and the summarizer is then left guessing at precisely
    the moment it was given this brief to stop guessing. So matched cards come
    first, and any remaining slots are filled with the other **character**
    cards. Places and items are not topped up: an unmentioned tavern is not
    who "she" was.

    Walking `adventure.story_cards` is a relationship load, which this module
    otherwise avoids. It is affordable here for two reasons the memory bank's
    own reads were not: a card is five short text columns with no vector, and
    this runs once per `MEMORY_INTERVAL` actions in the background task rather
    than on every turn. `build_context` already walks the same collection.
    """
    lines: list[str] = []
    name = adventure.persona_name.strip()
    pronouns = adventure.persona_pronouns.strip()
    if name or adventure.persona_desc.strip():
        who = f"{name} ({pronouns})" if name and pronouns else (name or "The player")
        lines.append(_cast_line(who, adventure.persona_desc.strip(), protagonist=True))

    seen = {name.lower()} if name else set()

    def add(card_name: str, entry: str) -> bool:
        """Adds one roster line. Returns False once the roster is full."""
        card_name = (card_name or "").strip()
        if card_name and card_name.lower() not in seen:
            seen.add(card_name.lower())
            lines.append(_cast_line(card_name, (entry or "").strip()))
        return len(lines) < MAX_CAST_MEMBERS

    room = True
    for card in match_cards(adventure.story_cards, text):
        room = add(card["name"], card["entry"])
        if not room:
            break
    if room:
        for card in adventure.story_cards:
            if (card.type or "").strip().lower() != "character":
                continue
            if not add(card.name, card.entry):
                break

    parts = []
    if lines:
        parts.append("Cast:\n" + "\n".join(lines))
    setting = adventure.memory.strip()
    if setting:
        parts.append("Setting:\n" + truncate_to_last_tokens(setting, SETTING_TOKENS))
    return "\n\n".join(parts)


# ---------- Retrieval (runs inside the turn, before build_context) ----------

async def retrieve_memories(
    adventure: models.Adventure,
    settings: models.Settings,
    *,
    update_stats: bool,
    exclude_action_id: int | None = None,
) -> dict | None:
    """Returns the memories to inject, or None when the bank is off.

    The result is a dict of the form
    `{"used": [{id, text, similarity, pinned}], "error": str | None}`. It is
    None when the memory bank is disabled for this adventure.

    Set `update_stats` to True to increment the use counters. Only real turns
    should do this, not the dry runs that Insights performs.

    `exclude_action_id` removes the action being retried from the similarity
    query, so that a discarded attempt cannot influence which memories are
    returned.
    """
    if not adventure.memory_bank_enabled:
        return None
    if not settings.embedding_model.strip():
        return {"used": [], "error": "No embedding model configured in Settings."}
    db = object_session(adventure)
    if db is None:
        return {"used": [], "error": None}

    # Select which memories are in play, and nothing else about them. This code
    # used to walk `adventure.memories`, which loaded every row of the bank,
    # including its vector. That cost about 31 KB per memory and about 3 MB per
    # turn, which was 96% of everything a turn read. An id and a flag come to
    # about eight bytes per row.
    #
    # The branch clause uses the whole lineage here rather than the window the
    # story is read through. Retrieval exists to recall events from far back in
    # the story, such as what happened forty turns ago. The full lineage stays
    # affordable because memories are sparse, at roughly one per six actions, so
    # even a heavily forked story returns only tens of small rows.
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
    # Pinned memories are always used, and they count toward `top_k`, so the
    # injected set stays within the budget unless the pinned memories alone
    # exceed it.
    top_k = max(1, settings.memory_top_k)
    used = [row for row in scored if row[2]]
    remaining = max(0, top_k - len(used))
    used += [row for row in scored if not row[2]][:remaining]
    used.sort(key=lambda row: row[0], reverse=True)
    if not used:
        return {"used": [], "error": None}

    # Fetch the text only now, and only for the `top_k` rows that were chosen.
    used_ids = [memory_id for _, memory_id, _ in used]
    texts = dict(
        db.execute(
            select(models.Memory.id, models.Memory.text)
            .where(models.Memory.id.in_(used_ids))
        ).all()
    )

    if update_stats:
        # Pass `synchronize_session=False` because nothing in this request
        # reads the counters back. Matching the UPDATE against loaded objects
        # would require loading those objects, which is the cost this code path
        # exists to avoid.
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
        # This code no longer clamps the cursors. Undo can leave the story
        # shorter than the mark. When the mark was a position, a value past the
        # end of the list stalled the pass until the story grew back, so every
        # post-turn run clamped it. That clamp introduced its own error, because
        # clamping to the settled count rewound a caught-up adventure by one
        # step and covered an action twice.
        #
        # An anchor past the tip is not an invalid value. `settled_after`
        # reports that there is nothing to do, and once the story grows past the
        # anchor the pass resumes where it stopped.
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
        # Re-read the anchor on every pass. Committing a memory does not change
        # the story, but this loop is the only code that moves the anchor, so
        # both numbers must be current.
        anchor = cursors.MEMORY.depth(db, adventure)
        if history.count_after(adventure, anchor) < MEMORY_INTERVAL:
            return  # No full block of story sits past the mark.
        if history.count(adventure) < MEMORY_START:
            return  # The adventure is too short to have started summarizing.
        # The order of those two checks is deliberate. The usual answer is that
        # no memory is due, and the first check settles that without measuring
        # the length of the whole story.
        block = history.after(adventure, anchor, MEMORY_INTERVAL)
        if len(block) < MEMORY_INTERVAL:
            return
        excerpt = truncate_to_last_tokens("\n\n".join(a.text for a in block), 2000)
        # Match the cast against the untruncated block. The excerpt is what the
        # model reads, but a character named in the part that was trimmed is
        # still one the memory may have to name.
        brief = cast_brief(adventure, "\n\n".join(a.text for a in block))
        prompt = f"Story excerpt:\n\n{excerpt}\n\nMemory:"
        try:
            text = await provider.complete(
                MEMORY_SYSTEM_PROMPT, f"{brief}\n\n{prompt}" if brief else prompt
            )
        except ProviderError:
            return  # Logged on the debug page. The cursor is unchanged, so the
                    # next turn retries this block.
        if not text:
            return
        memory = models.Memory(
            adventure_id=adventure.id,
            text=text,
            source_start=block[0].depth,
            source_end=block[-1].depth,
        )
        # Attach the memory to the node it summarizes, so that a fork inherits
        # the memories of the path it forked from and no others. Then move the
        # mark to that same node. Both values record how far this pass has
        # reached, and taking them from one row keeps them in step even when the
        # depths have gaps.
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
    # Where the summary stands once this run succeeds. Read this before the AI
    # call rather than after it. The mark records the end of the story as this
    # pass saw it, and a turn that arrives during the call must not be counted
    # as read.
    caught_up = history.newest(adventure)
    if caught_up is None:
        return

    # Include the memories for the stretch that the summary has not read, which
    # means every memory attached to a node past the anchor. The marks and the
    # memories are now depths on one path, so no coordinate conversion remains.
    # If memory creation has fallen behind, for example because the last attempt
    # failed, this falls back to the raw story text.
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
    # The summary is built from the memories, so it inherits their framing for
    # free once they are named and third-person. It still gets the brief of its
    # own, because the fallback above hands it raw second-person story text
    # whenever memory creation has fallen behind.
    brief = cast_brief(adventure, f"{current}\n\n{events_text}")
    user_prompt = (
        f"Current story summary:\n{current or '(none yet)'}\n\n"
        f"New events since the last update:\n{events_text}\n\n"
        "Updated summary:"
    )
    if brief:
        user_prompt = f"{brief}\n\n{user_prompt}"
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
    # Use a query rather than walking `adventure.memories`. That walk ran on
    # every turn and loaded the whole bank's vectors in order to find the few
    # rows with none.
    #
    # Neither this query nor the eviction below applies a branch clause, and
    # that is deliberate. Whether a row is embedded is a fact about the row, not
    # about the path being played. Skipping a sibling branch's memories would
    # only postpone the work until someone switched branches and needed them
    # ranked. Capacity works the same way. The bank belongs to the adventure,
    # and the memories of a branch nobody is reading are the right ones to evict
    # first.
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
    # The database performs both the count and the ranking, and returns neither
    # the rows nor the vectors. Counting by walking `adventure.memories` fetched
    # every vector in the bank on every turn, whether or not the bank was over
    # capacity.
    in_this_bank = (models.Memory.adventure_id == adventure.id,
                    models.Memory.forgotten.is_(False))
    active = db.execute(
        select(func.count(models.Memory.id)).where(*in_this_bank)
    ).scalar() or 0
    overflow = active - max(1, settings.memory_bank_capacity)
    if overflow <= 0:
        return
    # Evict the least recently used memory first, and use the use count only to
    # break ties.
    #
    # Ordering by use count first froze the bank. A memory written on this turn
    # has never been used, so once every other memory had been retrieved at
    # least once, the new memory held the lowest count in the bank. The same
    # post-turn run that wrote it then evicted it, one pass after embedding it.
    # Use counts only increase, so the bank never recovered. An adventure kept
    # whatever memories it held when the bank first filled, and every later
    # memory was summarized, marked as forgotten, and never ranked.
    #
    # Ordering by recency avoids that. A new memory carries the newest
    # timestamp, so it is the last row to be evicted rather than the first, and
    # it remains until other memories are used. Demoting the use count costs
    # little, because retrieving a useful memory also makes it recent. The two
    # orderings differ only for memories that were used once and have not been
    # retrieved since, which are the rows a full bank should evict.
    doomed = db.execute(
        select(models.Memory.id)
        .where(*in_this_bank, models.Memory.pinned.is_(False))
        .order_by(
            func.coalesce(models.Memory.last_used_at, models.Memory.created_at),
            models.Memory.use_count,
        )
        .limit(overflow)
    ).scalars().all()
    if not doomed:
        return  # Every active memory is pinned, so the pins override capacity.
    db.execute(
        update(models.Memory)
        .where(models.Memory.id.in_(doomed))
        .values(forgotten=True)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    # The bulk UPDATE bypassed the loaded objects, so code that still holds the
    # collection would otherwise see the evicted memories as active.
    db.expire(adventure, ["memories"])
