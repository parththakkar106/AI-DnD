"""Reading the story without reading all of it.

`story_actions()` walked `adventure.actions`, which loads every row of the
adventure — then every caller threw almost all of it away. The context builder
concatenates the story and immediately cuts it back to the token budget; the
NPC-in-scene check looks at the last 6; memory retrieval looks at the last 4;
the post-turn cursor clamp only wants a count. So a turn on a 200-action
adventure read ~840 KB to use maybe 70 KB of it, and the cost grew with every
turn played.

This module serves those shapes directly from SQL — a tail, a slice, a count —
so the read is bounded by the context budget instead of by the length of the
story.

Three rules hold everything together:

* **One definition of "story action".** Membership decides what a reader sees
  and what the summarizer is handed, so SQL and Python must agree on it
  exactly. `_STORY_TEXT` and `is_story_text()` are that one definition, written
  twice; keep them in step.
* **Never load twice.** If `adventure.actions` is already in memory (the
  scripting pipeline hands the whole history to user scripts, as AI Dungeon
  does), every helper here slices that instead of issuing a query, so a
  scripted adventure pays what it always paid and nothing more.
* **Every read goes through the branch clause** (Phase 14). `adventure.actions`
  is every branch's actions, not the story being played — so the shortcut above
  cuts the loaded collection down to the path before slicing it, exactly as the
  SQL does. This is the line that would silently assemble a prompt out of two
  different stories, which is why `lineage.Path` owns both halves of it.

Ordering is by `depth` now, not `index`. The two hold the same numbers until
retry stops mutating rows (SP4), but only one of them is a position along a
path.

SP3 added the reads that count *from a node* rather than from the start —
`count_after`, `after`, `newest_settled`. The memory bank used to ask for
"positions 12 to 18 of the story", which is a question whose answer moves when
an action is deleted from in front of it. It now asks for "the six actions
after depth 41", which is the same question a fork has to answer anyway.
"""

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session, defer, object_session

from .. import models
from . import lineage

# How many of the newest actions to read before checking whether the token
# budget is covered. When it isn't, the next size is worked out from the
# average action length just measured rather than by blind doubling — guessing
# high means reading hundreds of actions to use sixty of them.
WINDOW_START = 32
WINDOW_MARGIN = 0.15  # aim this far past the budget, so one more round is rare
WINDOW_STEP = 8  # ...and at least this many more actions each round

# Depth is the ordering key; id breaks the tie that a pre-tree row (depth NULL,
# and so invisible anyway) or a future sibling pair would otherwise leave to
# the database's mood.
_OLDEST_FIRST = (models.Action.depth, models.Action.id)
_NEWEST_FIRST = (models.Action.depth.desc(), models.Action.id.desc())


def _sql_stripped(column):
    """`column` with leading/trailing whitespace removed, portably.

    SQLite and Postgres both accept single-argument `trim()`, but it strips
    spaces only — Python's `.strip()` also drops newlines and tabs, and an
    action of nothing but a newline would otherwise count as story text here
    and not in Python. `replace()` and `trim()` are the two string functions
    both dialects spell identically, so fold the other whitespace into spaces
    first. (Form feed and vertical tab are not covered; nothing produces them.)
    """
    folded = column
    for char in ("\n", "\r", "\t"):
        folded = func.replace(folded, char, " ")
    return func.trim(folded)


_STORY_TEXT = _sql_stripped(models.Action.text) != ""


def is_story_text(text: str) -> bool:
    """The Python half of `_STORY_TEXT` — keep the two in step."""
    return bool(text.strip())


def _loaded_actions(adventure: models.Adventure) -> list[models.Action] | None:
    """The adventure's actions if they are already in memory, else None.

    Slicing an already-loaded collection is free; issuing a query beside it
    would mean paying for the same rows twice.
    """
    state = sa_inspect(adventure)
    if state.detached or "actions" in state.unloaded:
        return None
    return list(adventure.actions)


def _from_memory(
    adventure: models.Adventure, exclude_action_id: int | None
) -> list[models.Action] | None:
    """The story, from the already-loaded collection, or None to go to SQL.

    The collection is the *adventure's* actions — every branch of it. Cutting
    it down to the path here is the same filter the SQL applies, and skipping
    it would hand the context builder a prompt assembled from siblings of the
    story being played. The path needs a session to read the branch row from;
    without one there is no answer to give, so say so rather than guess.
    """
    loaded = _loaded_actions(adventure)
    if loaded is None:
        return None
    db = _session(adventure)
    if db is None:
        return None
    path = lineage.path_of(db, adventure)
    rows = [
        a for a in loaded
        if path.contains(a)
        and is_story_text(a.text)
        and (exclude_action_id is None or a.id != exclude_action_id)
    ]
    # `Adventure.actions` is ordered by `index`; a path is ordered by depth.
    rows.sort(key=path.sort_key)
    return rows


def _filters(
    adventure: models.Adventure,
    path: lineage.Path,
    exclude_action_id: int | None,
    entries: int | None = None,
) -> list:
    # adventure_id is redundant beside the branch clause — branch ids are
    # unique, so a branch already names one adventure. It stays because it is
    # the cheap half of the check that catches a node written onto the wrong
    # adventure's branch, and because a clause nobody can read is a clause
    # nobody maintains.
    conditions = [
        models.Action.adventure_id == adventure.id,
        path.clause(models.Action, count=entries),
        _STORY_TEXT,
    ]
    if exclude_action_id is not None:
        conditions.append(models.Action.id != exclude_action_id)
    return conditions


def _query(
    db: Session,
    adventure: models.Adventure,
    path: lineage.Path,
    exclude_action_id: int | None,
    entries: int | None = None,
):
    # Reasoning traces are never read from replayed history and can be larger
    # than the narration itself on a reasoning model.
    return (
        db.query(models.Action)
        .filter(*_filters(adventure, path, exclude_action_id, entries))
        .options(defer(models.Action.reasoning))
    )


def _count_query(
    db: Session,
    adventure: models.Adventure,
    path: lineage.Path,
    exclude_action_id: int | None,
    entries: int | None = None,
):
    """A real `SELECT count(...)`.

    Deliberately not `_query(...).count()`: that wraps the entity select in a
    subquery, so the emitted SQL names every column — including the deferred
    ones this whole design exists to keep off the wire. No bytes come back
    either way, but the database still has to read them, and an egress guard
    that greps the SQL cannot tell the two apart.
    """
    return db.query(func.count(models.Action.id)).filter(
        *_filters(adventure, path, exclude_action_id, entries)
    )


def _session(adventure: models.Adventure) -> Session | None:
    return object_session(adventure)


def _path(db: Session, adventure: models.Adventure) -> lineage.Path:
    return lineage.path_of(db, adventure)


# ------------------------------------------------------------------ the API

def story_actions(
    adventure: models.Adventure, exclude_action_id: int | None = None
) -> list[models.Action]:
    """Every story action, oldest first.

    Still the right call where the whole story is genuinely wanted — user
    scripts receive it, per AI Dungeon's scripting API. Prefer `tail`, `slice_`
    or `count` anywhere the caller only needs part of it.

    `exclude_action_id` drops one action from the story — used by retry, where
    the row being regenerated is still attached to the adventure (it holds the
    variant history) but must not appear in the context assembled to replace it.
    """
    in_memory = _from_memory(adventure, exclude_action_id)
    if in_memory is not None:
        return in_memory
    db = _session(adventure)
    if db is None:
        return []
    return (
        _query(db, adventure, _path(db, adventure), exclude_action_id)
        .order_by(*_OLDEST_FIRST)
        .all()
    )


def count(adventure: models.Adventure, exclude_action_id: int | None = None) -> int:
    """How many story actions there are, without fetching any of them."""
    in_memory = _from_memory(adventure, exclude_action_id)
    if in_memory is not None:
        return len(in_memory)
    db = _session(adventure)
    if db is None:
        return 0
    return (
        _count_query(db, adventure, _path(db, adventure), exclude_action_id).scalar()
        or 0
    )


def tail_range(
    adventure: models.Adventure,
    skip: int,
    limit: int,
    exclude_action_id: int | None = None,
) -> list[models.Action]:
    """`limit` story actions ending `skip` actions before the end, oldest first.

    `skip=0` is the newest slice; `skip=32, limit=16` is the 16 actions just
    older than the newest 32. Lets a growing window fetch only the part it
    doesn't already have.

    This is the read the lineage window exists for. The path's ranges are
    disjoint and descending, so the newest N nodes come from the newest few
    lineage entries and the rest of the ancestry need not be named at all: a
    story forked two hundred times reads its tail with as few clauses as one
    forked never. `prefix_covering` estimates how many entries that takes from
    depth arithmetic alone; the estimate is only ever short where a middle
    action was deleted, and then the read widens to the whole lineage and pays
    one more query.
    """
    if limit <= 0 or skip < 0:
        return []
    in_memory = _from_memory(adventure, exclude_action_id)
    if in_memory is not None:
        stop = len(in_memory) - skip
        return in_memory[max(stop - limit, 0):stop] if stop > 0 else []
    db = _session(adventure)
    if db is None:
        return []
    path = _path(db, adventure)
    entries = path.prefix_covering(skip + limit)
    while True:
        rows = (
            _query(db, adventure, path, exclude_action_id, entries)
            .order_by(*_NEWEST_FIRST)
            .offset(skip)
            .limit(limit)
            .all()
        )
        if len(rows) >= limit or entries >= len(path):
            break
        entries = len(path)  # short: widen once, to everything, and re-ask
    rows.reverse()
    return rows


def tail(
    adventure: models.Adventure, limit: int, exclude_action_id: int | None = None
) -> list[models.Action]:
    """The newest `limit` story actions, returned oldest first."""
    return tail_range(adventure, 0, limit, exclude_action_id)


def slice_(
    adventure: models.Adventure,
    start: int,
    length: int,
    exclude_action_id: int | None = None,
) -> list[models.Action]:
    """Story actions at positions [start, start + length), oldest first.

    Positions are into the same filtered, depth-ordered list the memory cursors
    count in, which is why the filter has to match Python's exactly.

    Counts from the oldest end, so it names the whole lineage: there is no
    prefix of the ancestry that holds "the story's first ten actions".
    """
    if length <= 0 or start < 0:
        return []
    in_memory = _from_memory(adventure, exclude_action_id)
    if in_memory is not None:
        return in_memory[start:start + length]
    db = _session(adventure)
    if db is None:
        return []
    return (
        _query(db, adventure, _path(db, adventure), exclude_action_id)
        .order_by(*_OLDEST_FIRST)
        .offset(start)
        .limit(length)
        .all()
    )


def depth_of(action: models.Action) -> int:
    """`action.depth`, with the no-depth case spelled once.

    A row with no depth is a pre-tree row, which no path contains — so it can
    only turn up in an already-loaded collection, and it sorts before the story
    rather than after it.
    """
    return action.depth if action.depth is not None else lineage.NO_DEPTH


def count_after(
    adventure: models.Adventure, depth: int, exclude_action_id: int | None = None
) -> int:
    """How many story actions lie past `depth` on the path.

    The node-anchored replacement for "the story is N long and the cursor is at
    M". Deleting an action from in front of the boundary makes this number
    smaller, which is true; it does not make the boundary point somewhere else,
    which is the bug the positions had.

    `covering_after` says exactly which lineage entries can hold a node deeper
    than the boundary, so a cursor near the tip names one branch however many
    forks are below it.
    """
    in_memory = _from_memory(adventure, exclude_action_id)
    if in_memory is not None:
        return sum(1 for a in in_memory if depth_of(a) > depth)
    db = _session(adventure)
    if db is None:
        return 0
    path = _path(db, adventure)
    return (
        _count_query(
            db, adventure, path, exclude_action_id, path.covering_after(depth)
        )
        .filter(models.Action.depth > depth)
        .scalar()
        or 0
    )


def after(
    adventure: models.Adventure,
    depth: int,
    limit: int,
    exclude_action_id: int | None = None,
) -> list[models.Action]:
    """The oldest `limit` story actions past `depth`, oldest first.

    "The next block the summarizer has not seen", asked as a fact about the
    story rather than as an offset into a list that shifts underneath it.
    """
    if limit <= 0:
        return []
    in_memory = _from_memory(adventure, exclude_action_id)
    if in_memory is not None:
        return [a for a in in_memory if depth_of(a) > depth][:limit]
    db = _session(adventure)
    if db is None:
        return []
    path = _path(db, adventure)
    return (
        _query(db, adventure, path, exclude_action_id, path.covering_after(depth))
        .filter(models.Action.depth > depth)
        .order_by(*_OLDEST_FIRST)
        .limit(limit)
        .all()
    )


def newest_settled(adventure: models.Adventure) -> models.Action | None:
    """The newest story action that is not the newest one — see
    `memorybank.settled_story_actions` for why one is always held back.

    Two rows, not a count and an offset: this is the node an anchor moves to
    when derived work catches up with the settled end of the story.
    """
    rows = tail(adventure, 2)
    return rows[0] if len(rows) == 2 else None


def max_action_index(adventure: models.Adventure) -> int:
    """Highest `Action.index` in the adventure, story text or not. -1 if empty.

    The one read here that is deliberately *not* path-scoped. `index` is the
    legacy column, kept unread until SP8 drops it, and its only remaining job
    is to hand the next row a number nothing else holds — which is a fact about
    the adventure, not about the story being played. Scoping it to a branch
    would let two branches issue the same index.
    """
    loaded = _loaded_actions(adventure)
    if loaded is not None:
        return max((a.index for a in loaded), default=-1)
    db = _session(adventure)
    if db is None:
        return -1
    highest = (
        db.query(func.max(models.Action.index))
        .filter(models.Action.adventure_id == adventure.id)
        .scalar()
    )
    return -1 if highest is None else highest


def window_covering(
    adventure: models.Adventure,
    budget_tokens: int,
    token_counter,
    exclude_action_id: int | None = None,
) -> list[models.Action]:
    """The newest story actions whose combined text exceeds `budget_tokens` —
    i.e. more than the context builder can possibly include, and never less.

    Measures rather than guesses a chars-per-token ratio, so the prompt is
    byte-for-byte what loading the whole story would have produced. Budgets on
    the raw text, which is never longer than the rendered history text, so
    erring here can only mean fetching slightly too much.

    Each round fetches only the actions it doesn't already hold, so no row is
    ever read twice however many rounds it takes.
    """
    actions: list[models.Action] = []
    tokens = 0
    size = WINDOW_START
    while True:
        older = tail_range(
            adventure, len(actions), size - len(actions), exclude_action_id
        )
        if not older:
            return actions  # already holding the whole story
        actions = older + actions
        tokens += sum(token_counter(a.text) for a in older)
        if len(actions) < size:
            return actions  # that was the whole story
        if tokens > budget_tokens:
            return actions
        # Short. Project how many actions the budget takes at the length these
        # ones turned out to be, and go straight there.
        average = tokens / len(actions)
        projected = int(budget_tokens / average * (1 + WINDOW_MARGIN)) + WINDOW_STEP
        size = max(projected, size + WINDOW_STEP)
