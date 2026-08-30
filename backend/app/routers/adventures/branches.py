"""The branch endpoints: list, rename, delete, and switch.

Attempts accumulate at the tip as siblings, which costs nothing. An attempt
becomes a branch only when the player continues the story from it and leaves the
line that moved past it. That is the same event as playing a turn past the
attempt. Creating the branch then rather than on the next turn means a branch
exists only for a divergence someone built on, and the line being left is not
modified.
"""

from fastapi import Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import attempts, models, schemas, tree
from ...context import cursors
from ...context import lineage
from ...database import get_db

from . import turns
from .deps import CurrentUser, current_adventure, router
from .nodes import db_tip
from .paging import current_window


@router.get("/{adventure_id}/branches", response_model=list[schemas.BranchOut])
def list_branches(
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Returns every branch of the adventure and where each one leaves its parent.

    A tree view is drawn from this shape. `fork_depth` gives the depth where the
    line splits off, and `depth` gives the depth where it currently ends. The
    whole picture costs one query over `branches` plus one grouped query over
    `actions`, never one query per branch, so a view of a hundred forks does not
    cost a hundred round trips.
    """
    branches = (
        db.query(models.Branch)
        .filter(models.Branch.adventure_id == adventure.id)
        .order_by(models.Branch.id)
        .all()
    )
    owned = {
        branch_id: (count, tip)
        for branch_id, count, tip in db.query(
            models.Action.branch_id,
            func.count(models.Action.id),
            func.max(models.Action.depth),
        )
        .filter(
            models.Action.adventure_id == adventure.id,
            models.Action.live.is_(True),
        )
        .group_by(models.Action.branch_id)
        .all()
    }
    out = []
    for branch in branches:
        count, tip = owned.get(branch.id, (0, None))
        out.append(schemas.BranchOut(
            id=branch.id,
            parent_branch_id=branch.parent_branch_id,
            fork_depth=branch.fork_depth,
            # A branch with no nodes of its own sits at its fork point. That
            # node is the last one its story contains. The node is borrowed, but
            # it is still the tip. This matches `tree.refresh_head`.
            depth=tip if tip is not None else (
                branch.fork_depth if branch.fork_depth is not None else tree.NO_DEPTH
            ),
            own_actions=count,
            is_head=(branch.id == adventure.head_branch_id),
            name=branch.name,
            created_at=branch.created_at,
        ))
    return out


def get_branch_or_404(
    adventure: models.Adventure, branch_id: int, db: Session
) -> models.Branch:
    """Returns one branch of this adventure.

    If the branch belongs to another adventure, the 404 does not confirm that the
    branch exists.
    """
    branch = db.get(models.Branch, branch_id)
    if branch is None or branch.adventure_id != adventure.id:
        raise HTTPException(404, "Branch not found")
    return branch


@router.patch(
    "/{adventure_id}/branches/{branch_id}", response_model=schemas.BranchOut
)
def rename_branch(
    branch_id: int,
    payload: schemas.BranchRename,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Names a branch, or clears the name to leave it unnamed.

    A blank string means the same thing as `null`. A name of only spaces is not a
    name anyone chose, and storing one gives the client an empty label to draw
    instead of the fork depth.
    """
    branch = get_branch_or_404(adventure, branch_id, db)
    name = (payload.name or "").strip()
    branch.name = name or None
    adventure.updated_at = models.utcnow()
    db.commit()
    db.refresh(branch)
    # Read both numbers in one pass, and count them the way `list_branches`
    # counts them, as live rows on this branch. A renamed branch is the same
    # branch, so this response has to match the row the panel would fetch.
    tip, own = (
        db.query(func.max(models.Action.depth), func.count(models.Action.id))
        .filter(
            models.Action.adventure_id == adventure.id,
            models.Action.branch_id == branch.id,
            models.Action.live.is_(True),
        )
        .one()
    )
    return schemas.BranchOut(
        id=branch.id,
        parent_branch_id=branch.parent_branch_id,
        fork_depth=branch.fork_depth,
        depth=tip if tip is not None else (
            branch.fork_depth if branch.fork_depth is not None else tree.NO_DEPTH
        ),
        own_actions=own,
        is_head=(branch.id == adventure.head_branch_id),
        name=branch.name,
        created_at=branch.created_at,
    )


@router.delete("/{adventure_id}/branches/{branch_id}", status_code=204)
def delete_branch(
    adventure_id: int,
    branch_id: int,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Deletes a branch and everything forked from it.

    Nothing prunes the tree automatically, so this endpoint is what keeps a
    heavily retried adventure from growing without bound. That is why it ships
    with the view that first lets anyone create a fork rather than after it.

    Two kinds of branch cannot be deleted. The root cannot, because it holds the
    turns every other branch borrows, so deleting it deletes the whole story. The
    branch currently being read cannot, and neither can any branch it was forked
    from, because the cascade would remove the head under the player and leave
    `head_branch_id` dangling. Switch branches first.

    Nodes and memories are deleted by `ON DELETE CASCADE`, and descendants by the
    cascade on `branches.parent_branch_id`, so the delete is a single statement
    however deep the subtree is.
    """
    branch = get_branch_or_404(adventure, branch_id, db)
    if branch.parent_branch_id is None:
        raise HTTPException(
            400, "This is the story's first branch — deleting it would delete "
                 "the adventure. Delete the adventure itself instead.",
        )
    head = db.get(models.Branch, adventure.head_branch_id)
    # The head's lineage lists itself and every branch it borrows from, so one
    # membership test covers both the branch being read and any branch forked
    # from it.
    if head is not None and branch.id in {
        entry_id for entry_id, _ in lineage.entries_of(head)
    }:
        raise HTTPException(
            400, "You are reading this branch, or one forked from it. Switch to "
                 "another branch first.",
        )
    turns.acquire_turn_lock(adventure_id)
    try:
        # Collect the subtree before the delete, because afterwards there is no
        # way to ask which branches were removed. A cursor left pointing at a
        # deleted branch is harmless on Postgres, which never reuses ids, but it
        # is a bug on SQLite, where the next fork can receive the id that was
        # just freed. A stale anchor then resolves onto a branch it never saw.
        doomed = _branch_subtree(db, adventure, branch)
        for cursor in cursors.ALL:
            stored_branch, _ = cursor.stored(adventure)
            if stored_branch in doomed:
                cursor.clear(adventure)
        # The deleted branch's memories are deleted with it, and their cached
        # vectors drop out of the catalogue on the next read, so no
        # invalidation call is needed. See the note on the `memorybank` cache.
        db.delete(branch)
        adventure.updated_at = models.utcnow()
        db.commit()
    finally:
        turns._active_turns.discard(adventure_id)

def _branch_subtree(
    db: Session, adventure: models.Adventure, root: models.Branch
) -> set[int]:
    """Returns `root` and every branch descended from it, following parent pointers.

    The walk runs over the adventure's own branch rows rather than one query per
    level. An adventure has few branches, so the walk costs one round trip, and a
    recursive CTE would have to be written twice for the two dialects this
    codebase supports.
    """
    children: dict[int | None, list[int]] = {}
    for bid, parent in db.query(models.Branch.id, models.Branch.parent_branch_id).filter(
        models.Branch.adventure_id == adventure.id
    ):
        children.setdefault(parent, []).append(bid)
    found: set[int] = set()
    stack = [root.id]
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(children.get(current, ()))
    return found


@router.post(
    "/{adventure_id}/branches/{branch_id}/switch", response_model=schemas.ActionPage
)
def switch_branch(
    adventure_id: int,
    branch_id: int,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Reads and plays a different branch of the story.

    No row is copied and no row is rewritten. The head pointer moves, and the
    shared script state and world state are restored to what that branch's tip
    left behind. The restore is what makes a switch safe. Both states are stored
    per adventure, so a branch that did not restore them would be played with
    another branch's numbers, including the world-state cooldown clock inside the
    snapshot.
    """
    branch = db.get(models.Branch, branch_id)
    if branch is None or branch.adventure_id != adventure.id:
        raise HTTPException(404, "Branch not found")
    turns.acquire_turn_lock(adventure_id)
    try:
        adventure.head_branch_id = branch.id
        tree.refresh_head(db, adventure)
        attempts.restore_state(adventure, db_tip(db, adventure))
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(adventure)
        return current_window(db, adventure)
    finally:
        turns._active_turns.discard(adventure_id)
