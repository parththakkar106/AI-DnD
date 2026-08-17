"""Phase 14 — putting nodes on the story tree.

The write half of the tree. Which branch a new node hangs off, what depth it
gets, and where an adventure's head points all live here, because every one of
them is the kind of thing that is silently wrong when it is spread across four
call sites: a node written without a branch is a node no read can see, and it
fails by disappearing rather than by raising.

The read half — the lineage clause that turns a branch into "this story" —
lands beside it in SP2 (`context/lineage.py`). Nothing here is read yet.

Until forking ships there is exactly one branch per adventure and `depth` is
the number `index` already held, so everything in this module is bookkeeping
that changes nothing observable. That is the point: by the time a read depends
on these columns, every row has them — including the rows written between the
two deploys, which no migration will ever visit.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

# The head depth of an adventure with no actions. Keeps "the next node goes at
# head_depth + 1" true with no special case, and mirrors migrations.NO_DEPTH.
NO_DEPTH = -1


def root_branch(db: Session, adventure: models.Adventure) -> models.Branch:
    """The adventure's root branch, created on first use.

    Get-or-create rather than created-with-the-adventure, because the adventures
    that need one most are the ones that already exist: a bundle being imported,
    a fixture built straight through the ORM, or a database whose migration ran
    before this code shipped.
    """
    branch = (
        db.query(models.Branch)
        .filter(
            models.Branch.adventure_id == adventure.id,
            models.Branch.parent_branch_id.is_(None),
        )
        .order_by(models.Branch.id)
        .first()
    )
    if branch is not None:
        return branch
    branch = models.Branch(adventure_id=adventure.id, lineage=[])
    db.add(branch)
    # The lineage names the branch's own id, so the row has to exist first.
    db.flush()
    branch.lineage = [[branch.id, None]]
    return branch


def head_branch(db: Session, adventure: models.Adventure) -> models.Branch:
    """The branch new nodes are played onto."""
    if adventure.head_branch_id is not None:
        branch = db.get(models.Branch, adventure.head_branch_id)
        if branch is not None:
            return branch
        # A head naming a branch that is gone is a bug somewhere else. Recover
        # onto the root instead of refusing to play — the alternative is an
        # adventure nobody can add to.
    branch = root_branch(db, adventure)
    adventure.head_branch_id = branch.id
    return branch


def place_action(
    db: Session, adventure: models.Adventure, action: models.Action
) -> models.Branch:
    """Put `action` on the head branch and move the head to it.

    `depth` follows `index` while the two coexist. They have to agree: a read
    ordering by depth and a cursor counting in index space are describing the
    same story, and SP2 swaps one for the other under everything at once.
    """
    branch = head_branch(db, adventure)
    action.branch_id = branch.id
    if action.depth is None:
        action.depth = action.index
    adventure.head_branch_id = branch.id
    if action.depth > adventure.head_depth:
        adventure.head_depth = action.depth
    return branch


def place_memory(
    db: Session, adventure: models.Adventure, memory: models.Memory
) -> models.Branch:
    """Attach a memory to the node that produced it.

    `source_end` is the index of the last action the memory summarises, which is
    that node's depth. A hand-written memory summarises nothing, so its depth
    stays NULL and it belongs to the adventure rather than to a path.
    """
    branch = head_branch(db, adventure)
    memory.branch_id = branch.id
    if memory.depth is None and memory.source_end is not None:
        memory.depth = memory.source_end
    return branch


def refresh_head(db: Session, adventure: models.Adventure) -> None:
    """Re-derive the head depth after nodes were removed (undo, delete).

    A branch with nothing on it sits at its fork point, because that is the last
    node its story contains — borrowed from the parent, but the tip all the
    same. A root branch with nothing on it has no story at all.
    """
    branch = head_branch(db, adventure)
    tip = (
        db.query(func.max(models.Action.depth))
        .filter(models.Action.branch_id == branch.id)
        .scalar()
    )
    if tip is None:
        tip = branch.fork_depth if branch.fork_depth is not None else NO_DEPTH
    adventure.head_depth = tip
