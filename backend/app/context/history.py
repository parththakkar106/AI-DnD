"""Reads part of a story without loading all of it.

`story_actions()` used to walk `adventure.actions`, which loads every row of the
adventure. Every caller then discarded nearly all of those rows. The context
builder joins the story and immediately trims it to the token budget. The
in-scene NPC check reads the last 6 actions. Memory retrieval reads the last 4.
The post-turn cursor clamp needs only a count. A turn on a 200-action adventure
read about 840 KB in order to use about 70 KB, and the cost grew with every
turn.

This module serves those shapes from SQL directly, as a tail, a slice, or a
count. A read is therefore bounded by the context budget rather than by the
length of the story.

Three rules hold the module together:

- There is one definition of a story action. That definition decides both what
  a reader sees and what the summarizer receives, so the SQL and the Python must
  agree exactly. `_STORY_TEXT` and `is_story_text()` express the same rule
  twice. Keep them in step.
- No caller loads the same rows twice. If `adventure.actions` is already in
  memory, every helper here slices that collection instead of running a query.
  The scripting pipeline hands the whole history to user scripts, as AI Dungeon
  does, so a scripted adventure costs no more than it did before.
- Every read applies the branch clause, as of Phase 14. `adventure.actions`
  holds the actions of every branch rather than the story being played, so the
  in-memory path filters the collection down to the path before slicing it, in
  the same way the SQL does. Skipping that filter would build a prompt from two
  different stories without reporting an error, which is why `lineage.Path`
  owns both forms of the rule.

Reads order by `depth` rather than `index`. Since SP4, two different rows can
hold the same value for both columns, because the attempts at one turn share
them. Only `depth` together with the `live` test in the branch clause
identifies the row the story uses.

SP3 added the reads that count from a node rather than from the start:
`count_after`, `after`, and `newest`. The memory bank used to ask for positions
12 through 18 of the story, and the answer to that question changes when an
action in front of those positions is deleted. It now asks for the six actions
after depth 41, which is the question that forking requires in any case.
"""

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session, defer, object_session

from .. import models
from . import lineage

# How many of the newest actions to read before checking whether they cover the
# token budget. If they do not, the next size comes from the average action
# length just measured rather than from doubling the previous size. Doubling
# overshoots, which means reading hundreds of actions in order to use sixty.
WINDOW_START = 32
WINDOW_MARGIN = 0.15  # Aim this far past the budget, so a second round is rare.
WINDOW_STEP = 8  # Read at least this many more actions in each round.

# `depth` is the ordering key, and `id` breaks ties. Without `id`, the database
# would choose the order. Two rows can share a depth: a pre-tree row, which has
# a NULL depth and is invisible to reads, or a pair of sibling attempts.
_OLDEST_FIRST = (models.Action.depth, models.Action.id)
_NEWEST_FIRST = (models.Action.depth.desc(), models.Action.id.desc())


def _sql_stripped(column):
    """`column` with leading/trailing whitespace removed, portably.

    SQLite and Postgres both accept `trim()` with a single argument, but that
    form removes spaces only. Python's `str.strip()` also removes newlines and
    tabs. Without this helper, an action containing only a newline would count
    as story text in SQL but not in Python. Both dialects spell `replace()` and
    `trim()` the same way, so this function converts the other whitespace to
    spaces first. It does not handle form feed or vertical tab, because nothing
    produces them.
    """
    folded = column
    for char in ("\n", "\r", "\t"):
        folded = func.replace(folded, char, " ")
    return func.trim(folded)


_STORY_TEXT = _sql_stripped(models.Action.text) != ""


def is_story_text(text: str) -> bool:
    """Returns whether `text` counts as story text.

    This is the Python form of `_STORY_TEXT`. Keep the two in step.
    """
    return bool(text.strip())


def _loaded_actions(adventure: models.Adventure) -> list[models.Action] | None:
    """The adventure's actions if they are already in memory, else None.

    Slicing a collection that is already loaded costs nothing, and running a
    query beside it would fetch the same rows a second time.
    """
    state = sa_inspect(adventure)
    if state.detached or "actions" in state.unloaded:
        return None
    return list(adventure.actions)


def _from_memory(
    adventure: models.Adventure, exclude_action_id: int | None
) -> list[models.Action] | None:
    """The story, from the already-loaded collection, or None to go to SQL.

    The collection holds the adventure's actions, which means the actions of
    every branch. Filtering it down to the path here applies the same rule that
    the SQL applies. Without that filter, the context builder would receive a
    prompt built from siblings of the story being played.

    Resolving the path requires a session to read the branch row from. If there
    is no session, this function returns None so that the caller falls back to
    SQL rather than guessing.
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
    # `adventure_id` is redundant beside the branch clause, because branch ids
    # are unique and a branch already identifies one adventure. The filter
    # remains because it costs little, it catches a node written onto another
    # adventure's branch, and it makes the query easier to read.
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

    This function deliberately avoids `_query(...).count()`. That form wraps the
    entity select in a subquery, so the emitted SQL names every column,
    including the deferred columns that this design keeps off the wire. Neither
    form returns those bytes to the client, but the database still reads them,
    and an egress guard that inspects the SQL cannot tell the two forms apart.
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

    Call this function when you need the whole story. User scripts receive it,
    which matches AI Dungeon's scripting API. Use `tail`, `slice_`, or `count`
    when you need only part of the story.

    `exclude_action_id` removes one action from the result. Retry uses it. The
    attempt being replaced is still the live node of its turn, because it stays
    live until a replacement exists, but it must not appear in the context that
    is assembled to replace it.
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

    Passing `skip=0` returns the newest slice. Passing `skip=32` and `limit=16`
    returns the 16 actions immediately older than the newest 32. A growing
    window therefore fetches only the actions it does not already hold.

    This read is the reason the lineage window exists. The path's ranges do not
    overlap and they descend, so the newest N nodes come from the newest few
    lineage entries and the query never has to name the rest of the ancestry. A
    story that has forked 200 times reads its tail with as few clauses as one
    that has never forked.

    `prefix_covering` estimates how many entries that takes, using depth
    arithmetic alone. The estimate falls short only when an action was deleted
    from the middle of the story. In that case this function widens the read to
    the whole lineage, at the cost of one more query.
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

    Positions index into the same filtered, depth-ordered list that the memory
    cursors count in, which is why the SQL filter must match the Python filter
    exactly.

    This function counts from the oldest end, so it names the whole lineage. No
    prefix of the ancestry contains the first ten actions of the story.
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

    A row with no depth predates the tree. No path contains such a row, so it
    appears only in a collection that is already loaded. It sorts before the
    story rather than after it.
    """
    return action.depth if action.depth is not None else lineage.NO_DEPTH


def count_after(
    adventure: models.Adventure, depth: int, exclude_action_id: int | None = None
) -> int:
    """How many story actions lie past `depth` on the path.

    This replaces the older calculation, which compared the length of the story
    with the position of the cursor. Deleting an action in front of the boundary
    makes this number smaller, which is correct. It does not move the boundary
    to a different action, which is the error that positions produced.

    `covering_after` reports which lineage entries can hold a node deeper than
    the boundary, so a cursor near the tip names one branch however many forks
    lie below it.
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

    This returns the next block that the summarizer has not read. It asks a
    question about the story rather than using an offset into a list whose
    entries move.
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


def newest(adventure: models.Adventure) -> models.Action | None:
    """The newest story action, or None on an empty story.

    This returns a row rather than a count and an offset, because it names the
    node an anchor moves to when derived work reaches the end of the story.

    It used to return the second newest action. The memory bank held one action
    back because retry rewrote a row, so a memory that covered the newest action
    could describe narration the player had already replaced. Since SP4, a retry
    writes a sibling row instead, and the derived work at a coordinate is
    withdrawn when the story at that coordinate changes. There is nothing left
    to hold back.
    """
    rows = tail(adventure, 1)
    return rows[0] if rows else None


def max_action_index(adventure: models.Adventure) -> int:
    """Highest `Action.index` in the adventure, story text or not. -1 if empty.

    This is the only read in the module that is deliberately not scoped to a
    path. `index` is a legacy column that remains unread until SP8 drops it. Its
    one remaining job is to give the next row a number that no other row holds,
    which is a fact about the adventure rather than about the story being
    played. Scoping the query to a branch would let two branches issue the same
    index.
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
    """Returns the newest story actions whose combined text exceeds `budget_tokens`.

    The result always holds at least as much text as the context builder can
    include, and never less.

    This function counts tokens rather than estimating a characters-per-token
    ratio, so the prompt matches what loading the whole story would produce. It
    budgets against the raw text, which is never longer than the rendered
    history text, so any error causes it to fetch slightly more than needed.

    Each round fetches only the actions it does not already hold, so no row is
    read twice however many rounds the loop takes.
    """
    actions: list[models.Action] = []
    tokens = 0
    size = WINDOW_START
    while True:
        older = tail_range(
            adventure, len(actions), size - len(actions), exclude_action_id
        )
        if not older:
            return actions  # The result already holds the whole story.
        actions = older + actions
        tokens += sum(token_counter(a.text) for a in older)
        if len(actions) < size:
            return actions  # That was the whole story.
        if tokens > budget_tokens:
            return actions
        # The window is still short. Estimate how many actions the budget needs
        # at the average length just measured, then read that many.
        average = tokens / len(actions)
        projected = int(budget_tokens / average * (1 + WINDOW_MARGIN)) + WINDOW_STEP
        size = max(projected, size + WINDOW_STEP)
