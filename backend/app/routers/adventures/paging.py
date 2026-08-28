"""Reading a window of actions, and numbering the attempts inside it.

Three callers build an action window: the adventure GET, the action list, and
every endpoint that returns a page after changing the story. They read the same
columns and apply the same numbering, so both live here.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from ... import models, schemas
from ...context import lineage


# The columns `schemas.ActionOut` renders, listed explicitly.
#
# `deferred=True` in `models.py` keeps the four heavy columns out of bulk reads,
# but each new column then has to opt in to staying narrow. Both egress
# regressions this project has had came from a column that did not opt in. This
# tuple inverts the default: a new column costs nothing until you add it here.
#
# `world_delta` is listed because `ActionOut.world_changes` is computed from it.
# Omitting it saves no bytes. It converts one bulk read into one lazy load per
# row.
ACTION_LIST_COLUMNS = (
    models.Action.adventure_id,
    models.Action.index,
    models.Action.type,
    models.Action.text,
    models.Action.reasoning,
    models.Action.world_delta,
    models.Action.variant_count,
    models.Action.variant_index,
    # SP9: the pager's key. If `parent_id` were deferred, every row on the page
    # would cost a lazy load, which is the cost `load_only` is here to prevent.
    # `branch_id` is listed for the same reason. The pager reads it to tell a
    # local step from a branch switch.
    models.Action.parent_id,
    models.Action.branch_id,
    models.Action.created_at,
)


# How many actions an adventure opens with, and how many arrive per scroll.
#
# Opening a finished adventure once fetched the whole story in one response.
# That reached 589.5 kB for the longest story in production, and it grew as
# stories grew. 60 actions is a few screens of reading. The common case of
# opening a story, reading the end, and taking a turn never pages, and the worst
# case is bounded by the window size rather than by the length of the story.
ACTION_PAGE = 60


def action_window(
    db: Session,
    adventure: models.Adventure,
    before_id: int | None = None,
    limit: int = ACTION_PAGE,
) -> tuple[list[models.Action], int, bool]:
    """Returns the `limit` actions immediately older than `before_id`, oldest first.

    The return value is `(actions, total, has_more)`. If `before_id` is `None`,
    the newest window is returned.

    The query is scoped to the head branch's lineage, which is the story being
    played, rather than to the adventure. A sibling branch's turns therefore
    never appear in the transcript. `total` counts the same path, because it is
    what tells the reader that more actions exist above.

    The window is anchored on an action, never on a count or on arithmetic over
    depth, for two reasons:

    * Appends. Counting back from the newest action shifts every older position
      when a turn lands. A reader who scrolls up while a turn is generating gets
      a window that is one row off, which re-sends one action and skips another.
      An anchor is stable, because "older than this action" means the same thing
      before and after the story grows.
    * The story tree. Depth is dense today, and branching ends that. Comparing
      depths to order a path still works, but treating them as positions does
      not.

    `has_more` comes from requesting one row past the window rather than from a
    second count, so it costs one row instead of a scan.
    """
    path = lineage.path_of(db, adventure)
    on_path = (
        models.Action.adventure_id == adventure.id,
        path.clause(models.Action),
    )
    total = db.query(func.count(models.Action.id)).filter(*on_path).scalar()
    if limit <= 0:
        return [], total, total > 0

    query = db.query(models.Action).options(load_only(*ACTION_LIST_COLUMNS)).filter(*on_path)
    if before_id is not None:
        anchor = (
            db.query(models.Action.depth)
            .filter(models.Action.id == before_id, *on_path)
            .scalar()
        )
        if anchor is None:
            # The anchor was deleted while the reader scrolled, by an undo or
            # by an edited turn, or it belongs to a branch this story is not on.
            # No row can be older than a row that is not present, so report the
            # end of the story rather than guess and return a duplicate page.
            return [], total, False
        query = query.filter(models.Action.depth < anchor)

    rows = (
        query.order_by(models.Action.depth.desc(), models.Action.id.desc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, total, has_more


def annotate_takes(
    db: Session, adventure_id: int, actions: list[models.Action]
) -> list[models.Action]:
    """Sets the `2/4` pager numbers on every action on a page (SP9).

    This runs one query for the whole page rather than one per row. The pager
    needs the shape of each turn's attempt group, and calling `attempts.group`
    per action costs one query per message on screen. `variant_count` was cached
    to avoid that cost, which is why SP8 could not drop it.

    This function reads the siblings rather than counting them. A group holds
    only a few attempts, the page is bounded, and a count still needs a second
    query for the ordinal. It fetches only the id and the ordering keys, so it
    stays cheap even when the text is large.
    """
    parents = {a.parent_id for a in actions if a.parent_id is not None}
    if parents:
        rows = (
            db.query(
                models.Action.id,
                models.Action.parent_id,
                models.Action.variant_index,
            )
            .filter(
                models.Action.adventure_id == adventure_id,
                models.Action.parent_id.in_(parents),
            )
            .order_by(models.Action.variant_index, models.Action.id)
            .all()
        )
    else:
        rows = []
    siblings: dict[int, list[int]] = {}
    for row_id, parent_id, _ in rows:
        siblings.setdefault(parent_id, []).append(row_id)
    for action in actions:
        ids = siblings.get(action.parent_id) if action.parent_id else None
        if not ids:
            # A root node, or a pre-SP9 row that the backfill could not place.
            # It has one attempt, which is how it was written.
            action.take_count, action.take_index = 1, 0
            continue
        action.take_count = len(ids)
        action.take_index = ids.index(action.id) if action.id in ids else 0
    return actions


def current_window(db: Session, adventure: models.Adventure) -> schemas.ActionPage:
    actions, total, has_more = action_window(db, adventure)
    return schemas.ActionPage(
        actions=[
            schemas.ActionOut.model_validate(a)
            for a in annotate_takes(db, adventure.id, actions)
        ],
        total=total,
        has_more=has_more,
    )
