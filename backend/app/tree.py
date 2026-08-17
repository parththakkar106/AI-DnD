"""Phase 14 — putting nodes on the story tree.

The write half of the tree. Which branch a new node hangs off, what depth it
gets, and where an adventure's head points all live here, because every one of
them is the kind of thing that is silently wrong when it is spread across four
call sites: a node written without a branch is a node no read can see, and it
fails by disappearing rather than by raising.

The read half — the lineage clause that turns a branch into "this story" —
lives beside it in `context/lineage.py`.

Until forking ships there is exactly one branch per adventure and `depth` is
the number `index` already held, so everything in this module is bookkeeping
that changes nothing observable. That is the point: by the time a read depends
on these columns, every row has them — including the rows written between the
two deploys, which no migration will ever visit.

SP2 added `place_new_nodes`, which the session calls on every flush. Wiring the
call sites was enough while nothing read the columns; now that reads select on
them, "every writer remembers" is a promise that has to hold for every fixture,
script and test ever written too, and its breach is a story quietly missing
turns. So the invariant is enforced at the flush instead of asked for.
"""

import copy

from sqlalchemy import func, insert, update
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
    # Inserted through Core rather than through the unit of work, because this
    # also runs from `place_new_nodes` inside a flush, and a nested ORM flush
    # inside a flush raises. Same transaction either way, so it rolls back with
    # everything else. The lineage names the branch's own id, so it takes a
    # second statement — once per adventure, ever.
    new_id = db.execute(
        insert(models.Branch).values(
            adventure_id=adventure.id,
            parent_branch_id=None,
            fork_depth=None,
            lineage=[],
            created_at=models.utcnow(),
        )
    ).inserted_primary_key[0]
    db.execute(
        update(models.Branch)
        .where(models.Branch.id == new_id)
        .values(lineage=[[new_id, None]])
    )
    return db.get(models.Branch, new_id)


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
    db: Session,
    adventure: models.Adventure,
    action: models.Action,
    branch: models.Branch | None = None,
) -> models.Branch:
    """Put `action` on the head branch and move the head to it.

    `depth` follows `index` while the two coexist. They have to agree: a read
    ordering by depth and a cursor counting in index space are describing the
    same story, and SP2 swaps one for the other under everything at once.

    `branch` is the head, already resolved, for a caller placing several nodes
    at once — see `place_new_nodes` for why that is worth a parameter.
    """
    branch = branch or head_branch(db, adventure)
    action.branch_id = branch.id
    if action.depth is None:
        action.depth = action.index
    adventure.head_branch_id = branch.id
    if action.depth is not None and action.depth > adventure.head_depth:
        adventure.head_depth = action.depth
    return branch


def place_memory(
    db: Session,
    adventure: models.Adventure,
    memory: models.Memory,
    branch: models.Branch | None = None,
) -> models.Branch:
    """Attach a memory to the node that produced it.

    `source_end` is the index of the last action the memory summarises, which is
    that node's depth. A hand-written memory summarises nothing, so its depth
    stays NULL and it belongs to the adventure rather than to a path.
    """
    branch = branch or head_branch(db, adventure)
    memory.branch_id = branch.id
    if memory.depth is None and memory.source_end is not None:
        memory.depth = memory.source_end
    return branch


def attach_memory(memory: models.Memory, node: models.Action) -> None:
    """Hang a memory off the node it was derived from.

    The general rule, of which the memory bank is the first instance: anything
    derived from the story attaches to the node that produced it, and is then
    visible from exactly the paths that node is on. A fork inherits its
    ancestors' memories because it inherits their nodes — nothing is copied and
    nothing is recreated — and a memory made on a sibling is invisible here
    because that node is not on this path.

    Not `place_memory`: this takes the branch from the *node*, which is not
    always the head. A block of story can end before the fork the current
    branch was made at, and the memory belongs where the ground is.
    """
    memory.branch_id = node.branch_id
    memory.depth = node.depth


def stamp_outcome(adventure: models.Adventure, action: models.Action) -> None:
    """Give a node the state it left behind, if its writer did not.

    The floor under `attempts.snapshot_outcome`, and it is here for the same
    reason `place_action` has one: from SP4 a node with no outcome is a node
    undo and retry cannot roll back past, and it fails by leaving the
    scoreboard where it was rather than by raising. The turn engine records the
    outcome itself and this skips those rows; what it catches is every fixture,
    script and import that writes a story straight through the ORM.

    What it writes is the truth as of the flush: a writer that changes no state
    between two nodes leaves the same state behind both of them.
    """
    if action.state_after is None:
        state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
        action.state_after = copy.deepcopy(state)
    if action.world_state_after is None:
        world = adventure.world_state if isinstance(adventure.world_state, dict) else {}
        action.world_state_after = copy.deepcopy(world)


def place_new_nodes(session: Session) -> None:
    """Place every unplaced node about to be inserted. Runs on every flush.

    The call sites still call `place_action` / `place_memory` themselves, and
    should: a node placed at the call site is placed *before* the code around
    it reads the row back, and the explicit call is what makes the ordering
    visible. This is the floor under them — a fixture built straight through
    the ORM, a script, a test, or a call site added next year gets a branch
    without knowing the tree exists.

    Nodes whose adventure has not been inserted yet are left alone: there is no
    id to hang a branch off, and an Action needs `adventure_id` to be written
    at all, so the case does not arise from any writer we have.

    The head is resolved once per adventure per flush, and held in `heads` for
    the length of the call. That is not just saving a dictionary lookup: the
    identity map holds *weak* references, so a branch row nobody keeps a strong
    reference to is collected between two nodes and read back from the database
    for the next one. Resolving per node turned a fixture writing two hundred
    actions in one flush into two hundred SELECTs on `branches`.
    """
    heads: dict[int, models.Branch] = {}
    for obj in list(session.new):
        if isinstance(obj, models.Action):
            place = place_action
        elif isinstance(obj, models.Memory):
            place = place_memory
        else:
            continue
        if obj.adventure_id is None:
            continue
        adventure = session.get(models.Adventure, obj.adventure_id)
        if adventure is None:
            continue
        if isinstance(obj, models.Action):
            stamp_outcome(adventure, obj)
        if obj.branch_id is not None:
            continue  # already placed at its call site
        head = heads.get(adventure.id)
        if head is None:
            head = heads[adventure.id] = head_branch(session, adventure)
        place(session, adventure, obj, head)


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
