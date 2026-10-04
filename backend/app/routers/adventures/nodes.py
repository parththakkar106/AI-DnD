"""Moving around the story tree: what is newest, what comes next, what to remove.

These functions answer questions about action nodes without knowing which
endpoint asked. They do not touch the turn lock and they do not stream, so any
module in the package can import them.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session, undefer

from ... import attempts, memorybank, models, tree
from ...context import cursors
from ...context import lineage
from .deps import get_row_or_404


def next_depth(adventure: models.Adventure) -> int:
    """Returns the depth for the next node played onto this story, one past the tip.

    A depth is a position along one story, and the branch is what makes it
    unambiguous. Two branches each hold a node at depth 4, and they are
    alternatives rather than duplicates.
    """
    return adventure.head_depth + 1


def last_action(adventure: models.Adventure, db: Session) -> models.Action | None:
    """Returns the newest action of any kind on the story being played, or `None`.

    This runs a query rather than reading `adventure.actions[-1]`, which loads
    the entire story to read one row. That collection also holds every branch's
    actions, so it sometimes returns a row from the wrong branch.
    """
    return (
        db.query(models.Action)
        .filter(
            models.Action.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Action),
        )
        .order_by(models.Action.depth.desc(), models.Action.id.desc())
        .first()
    )


def _move_to_after(
    db: Session, adventure: models.Adventure, after_id: int | None
) -> None:
    """Moves the story to `after_id` before the turn is played.

    This is where a branch is created (SP9). Reading an attempt that the story
    moved past changes nothing on the server. Writing below one is the first time
    the player states which line they mean, and that is when the fork happens.

    An attempt already on the path needs no move, because the story is already
    there.
    """
    if after_id is None:
        return
    node = get_row_or_404(db, models.Action, after_id, adventure, "Action")
    if node.live:
        if lineage.path_of(db, adventure).contains(node):
            return
        # The take is the story on another branch. The pager switches branches
        # instead of offering such a take, but a second tab can still send one
        # it previewed before another tab forked it. Standing on it would move
        # that branch's live row away, and that branch's story would lose the
        # turn.
        raise HTTPException(
            400,
            "That take is already the story on another branch. Switch to that "
            "branch to read it.",
        )
    if len(attempts.group(db, node)) < 2:
        # The pager cannot reach this node, so no legitimate action put the
        # player here.
        raise HTTPException(400, "That take is not one of this turn's.")
    stand_on(db, adventure, node)
    db.commit()
    db.refresh(adventure)


def delete_turn(
    db: Session, adventure: models.Adventure, node: models.Action
) -> None:
    """Removes a turn, including every attempt at it and not only the one on screen.

    A discarded attempt is a leaf at the same coordinate, and the only way to
    reach it is through that coordinate. Leaving it behind when the turn is
    deleted orphans a row that no read can reach. Whatever the turn produced is
    withdrawn once, because a memory is attached to the coordinate rather than to
    one attempt.
    """
    memorybank.forget_node(db, adventure, node)
    # Scoped to this node's branch (SP9). Groups span branches now, and an
    # attempt forked onto its own line belongs to another branch's story. See
    # `attempts.on_branch`.
    for attempt in attempts.on_branch(attempts.group(db, node), node):
        db.delete(attempt)


def db_tip(db: Session, adventure: models.Adventure) -> models.Action | None:
    """Returns the newest node of the story as it stands, with its outcome loaded."""
    return (
        db.query(models.Action)
        .filter(
            models.Action.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Action),
        )
        .options(
            undefer(models.Action.state_after),
            undefer(models.Action.world_state_after),
        )
        .order_by(models.Action.depth.desc(), models.Action.id.desc())
        .first()
    )


def stand_on(
    db: Session, adventure: models.Adventure, action: models.Action
) -> None:
    """Makes `action` the attempt the story tells, forking only if that is needed.

    There are two cases, and the caller does not have to know which one applies.
    While the turn is still the tip, its attempts are leaves that nothing was
    built on, so this is a switch and no branch is created. Once the story has
    moved past the turn, the line being left keeps every turn it has, so the
    attempt needs a branch of its own.

    A turn played below an attempt calls this function before it writes (SP9).
    """
    newest = last_action(adventure, db)
    at_the_tip = (
        newest is not None
        and newest.branch_id == action.branch_id
        and newest.depth == action.depth
    )
    if at_the_tip:
        # The story at this coordinate is about to change, so withdraw whatever
        # was derived from it. A retry does the same thing. A fork needs none of
        # this, because it leaves the coordinate and its memory where they are.
        # See `tree.fork`.
        memorybank.forget_node(db, adventure, action)
        cursors.rewind_all(adventure, action.branch_id, (action.depth or 0) - 1)
        attempts.make_live(db, adventure, action)
    else:
        tree.fork(db, adventure, action)
        attempts.restore_state(adventure, action)
