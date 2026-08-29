"""Phase 14: writes nodes onto the story tree.

This module is the write half of the tree. It decides which branch a new node
goes on, what depth the node gets, and where the adventure's head points. All
three decisions live here because a mistake in any of them is silent. A node
written without a branch is invisible to every read, and nothing raises an
error.

The read half is `context/lineage.py`. It turns a branch into the set of nodes
that make up one story.

Until forking ships, each adventure has one branch and `depth` mirrors `index`,
so nothing here changes observable behavior yet. That is intentional. By the
time reads depend on these columns, every row already has them, including the
rows written between the two deploys that no migration visits.

SP2 added `place_new_nodes`, which runs on every flush. Wiring up individual
call sites worked while nothing read the columns. Now that reads filter on them,
relying on each writer to remember would also mean relying on every fixture,
script, and test. A missed call produces a story with missing turns, so the
flush enforces the rule instead.
"""

import copy

from sqlalchemy import func, insert, update
from sqlalchemy.orm import Session

from . import models
from .context import lineage

# Head depth of an adventure that has no actions. Using -1 keeps the rule "the
# next node goes at head_depth + 1" true without a special case. This matches
# `migrations.NO_DEPTH`.
NO_DEPTH = -1


def root_branch(db: Session, adventure: models.Adventure) -> models.Branch:
    """Returns the adventure's root branch, creating it on first use.

    This function gets or creates the branch instead of creating it alongside
    the adventure. The adventures that need a root branch are usually ones that
    already exist: an imported bundle, a fixture built through the ORM, or a
    database migrated before this code shipped.
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
    # Use a Core insert instead of the ORM. `place_new_nodes` can call this
    # during a flush, and a nested ORM flush raises an error. Both paths share
    # one transaction. The lineage refers to the branch's own id, so it needs a
    # second statement, which runs once per adventure.
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
    """Returns the branch that new nodes are played onto."""
    if adventure.head_branch_id is not None:
        branch = db.get(models.Branch, adventure.head_branch_id)
        if branch is not None:
            return branch
        # The head points at a branch that no longer exists, which means a bug
        # elsewhere. Fall back to the root instead of refusing to play,
        # otherwise the adventure becomes unusable.
    branch = root_branch(db, adventure)
    adventure.head_branch_id = branch.id
    return branch


def fork(db: Session, adventure: models.Adventure, node: models.Action) -> models.Branch:
    """Moves `node` onto a new branch so the story can continue from it.

    `node` is a discarded attempt at a turn that the story has already moved
    past. Making it live where it stands would orphan every turn played after
    it, because those turns continue the attempt that won. Instead, `node` moves
    to a new branch that forks from the depth just before it. The parent branch
    keeps its story unchanged. The new branch inherits everything up to the fork
    and owns this one node.

    This function inserts one row and moves one row. It copies nothing, so a
    fork costs one `branches` row plus the ancestry cached on it, regardless of
    how long the story is.

    Derived data stays on the parent, by design. A memory attaches to the node
    its block ends on, and that node does not move. From the new branch, the
    lineage caps the parent at `fork_depth`, so the memory sits one depth past
    the border. Neither retrieval nor the cursors can see it, and the block is
    summarized again from the text this branch contains.

    The other attempts at this turn also stay on the parent, because they are
    still takes on the parent's turn. If none of them is live, the oldest one
    becomes live, so the parent keeps the line it was written on.
    """
    parent = db.get(models.Branch, node.branch_id)
    if parent is None or node.depth is None:
        raise ValueError("cannot fork from a node that is not on a branch")
    fork_depth = node.depth - 1
    # Read the sibling attempts before moving the node. The session does not
    # autoflush, so a later read still finds the node here and hands the live
    # flag back to it.
    remaining = [
        row for row in db.query(models.Action)
        .filter(
            models.Action.adventure_id == adventure.id,
            models.Action.branch_id == parent.id,
            models.Action.depth == node.depth,
        )
        .order_by(models.Action.id)
        .all()
        if row is not node
    ]
    # The parent's ancestry, with every entry capped at the fork depth. Only the
    # first entry can change in practice, because older entries are already
    # capped at a shallower depth. Capping all of them states the invariant
    # directly.
    inherited = [
        [branch_id, fork_depth if cap is None else min(cap, fork_depth)]
        for branch_id, cap in lineage.entries_of(parent)
    ]
    # Core insert with the lineage written second, for the reason given in
    # `root_branch`. This code can run inside a flush, and the lineage refers to
    # the new row's own id.
    new_id = db.execute(
        insert(models.Branch).values(
            adventure_id=adventure.id,
            parent_branch_id=parent.id,
            fork_depth=fork_depth,
            lineage=[],
            created_at=models.utcnow(),
        )
    ).inserted_primary_key[0]
    db.execute(
        update(models.Branch)
        .where(models.Branch.id == new_id)
        .values(lineage=[[new_id, None]] + inherited)
    )

    depth = node.depth
    node.branch_id = new_id
    node.live = True

    # The group the node left needs a live attempt again. Taking the oldest is
    # arbitrary, and it has to be somebody: a coordinate with no live attempt
    # disappears from the story on the branch it was left on.
    if remaining and not any(row.live for row in remaining):
        remaining[0].live = True

    adventure.head_branch_id = new_id
    adventure.head_depth = depth
    return db.get(models.Branch, new_id)


def branch_at(
    db: Session, adventure: models.Adventure, fork_depth: int
) -> models.Branch:
    """Creates an empty branch that leaves the current path at `fork_depth`.

    `fork` moves an existing node onto its own branch. This function creates the
    same kind of branch with no nodes on it yet, for the case where the take
    that will live there does not exist. A player asking for another take of a
    turn the story has moved past reaches this path (SP9).

    The head lands at `fork_depth`, so the next node written becomes the new
    take. That node gets the same depth as the original and the same parent,
    which `place_action` derives from the path.

    This function does not modify the branch being left. That branch keeps its
    node at that depth, the node stays live, and every turn played after it
    stays in place.
    """
    parent = head_branch(db, adventure)
    inherited = [
        [branch_id, fork_depth if cap is None else min(cap, fork_depth)]
        for branch_id, cap in lineage.entries_of(parent)
    ]
    # Core insert with the lineage written second, for the reason given in
    # `root_branch`. This code can run inside a flush, and the lineage refers to
    # the new row's own id.
    new_id = db.execute(
        insert(models.Branch).values(
            adventure_id=adventure.id,
            parent_branch_id=parent.id,
            fork_depth=fork_depth,
            lineage=[],
            created_at=models.utcnow(),
        )
    ).inserted_primary_key[0]
    db.execute(
        update(models.Branch)
        .where(models.Branch.id == new_id)
        .values(lineage=[[new_id, None]] + inherited)
    )
    adventure.head_branch_id = new_id
    adventure.head_depth = fork_depth
    return db.get(models.Branch, new_id)


def place_action(
    db: Session,
    adventure: models.Adventure,
    action: models.Action,
    branch: models.Branch | None = None,
    parent: models.Action | None = None,
) -> models.Branch:
    """Puts `action` on the head branch and moves the head to it.

    An action with no `depth` of its own goes one step past the tip, which is
    where the next turn belongs. The opening of a new adventure lands at 0 that
    way, because an adventure with nothing played has a head depth of
    `NO_DEPTH`.

    Pass `branch` when you have already resolved the head and are placing
    several nodes at once. See `place_new_nodes` for why that is worth doing.

    `parent` is the take that this node was played after (SP9), and it is what
    groups the takes of one turn. If you omit it, this function derives it from
    the path, which is the correct default: a node written now follows the story
    the player is reading now. If you place several nodes in one flush, chain
    `parent` explicitly, because the database has not seen any of them yet.
    """
    branch = branch or head_branch(db, adventure)
    action.branch_id = branch.id
    if action.depth is None:
        action.depth = adventure.head_depth + 1
    if parent is not None:
        action.parent_id = parent.id
    elif action.parent_id is None and action.depth:
        action.parent_id = _preceding_id(db, adventure, branch, action.depth)
    adventure.head_branch_id = branch.id
    if action.depth is not None and action.depth > adventure.head_depth:
        adventure.head_depth = action.depth
    return branch


def _preceding_id(
    db: Session, adventure: models.Adventure, branch: models.Branch, depth: int
) -> int | None:
    """Returns the id of the live node one step back along `branch`'s path.

    The query searches the whole lineage instead of `branch` alone, because a
    branch inherits the story before its fork point. The node in front of a
    forked branch's first turn lives on an ancestor, and that node is the parent
    a pager needs in order to find siblings.
    """
    path = lineage.Path(lineage.entries_of(branch))
    return (
        db.query(models.Action.id)
        .filter(
            models.Action.adventure_id == adventure.id,
            models.Action.depth == depth - 1,
            models.Action.live.is_(True),
            path.clause(),
        )
        .order_by(models.Action.id)
        .limit(1)
        .scalar()
    )


def place_memory(
    db: Session,
    adventure: models.Adventure,
    memory: models.Memory,
    branch: models.Branch | None = None,
) -> models.Branch:
    """Attaches a memory to the node it belongs to.

    `source_end` is the index of the last action the memory summarizes, which is
    that node's depth. A hand-written memory summarizes no actions, so it uses
    the head instead. That records the story the author was reading at the time.

    Giving every memory a coordinate is what makes its scope unambiguous (SP7).
    Before SP7, hand-written memories had a NULL depth and belonged to the
    adventure rather than to a path. A fork cannot cap a NULL, so those memories
    followed the reader onto branches whose story they did not describe. Now the
    question "is this memory part of the story I am reading?" has one answer for
    every row in the bank, and it is the same answer the lineage gives for
    nodes.
    """
    branch = branch or head_branch(db, adventure)
    memory.branch_id = branch.id
    if memory.depth is None:
        memory.depth = (
            memory.source_end if memory.source_end is not None
            else adventure.head_depth
        )
    return branch


def attach_memory(memory: models.Memory, node: models.Action) -> None:
    """Attaches a memory to the node it was derived from.

    This follows the general rule for derived data, and the memory bank is the
    first case of it. Anything derived from the story attaches to the node that
    produced it, and is then visible from exactly the paths that contain that
    node. A fork inherits its ancestors' memories because it inherits their
    nodes, so nothing is copied. A memory made on a sibling branch is not
    visible, because that node is not on this path.

    This function differs from `place_memory` because it takes the branch from
    `node`, which is not always the head. A block of story can end before the
    fork that created the current branch, and the memory belongs where that
    block is.
    """
    memory.branch_id = node.branch_id
    memory.depth = node.depth


def stamp_outcome(adventure: models.Adventure, action: models.Action) -> None:
    """Records the state a node left behind, if the writer did not record it.

    This is the fallback under `attempts.snapshot_outcome`, and it exists for
    the same reason `place_action` has one. Since SP4, undo and retry cannot
    roll back past a node that has no outcome, and the failure is silent: the
    script state and world state stay where they were. The turn engine records
    the outcome itself, so this function skips those rows. It catches fixtures,
    scripts, and imports that write a story directly through the ORM.

    The values written are the state as of the flush. That is correct, because a
    writer that changes no state between two nodes leaves the same state behind
    both of them.
    """
    if action.state_after is None:
        state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
        action.state_after = copy.deepcopy(state)
    if action.world_state_after is None:
        world = adventure.world_state if isinstance(adventure.world_state, dict) else {}
        action.world_state_after = copy.deepcopy(world)


def place_new_nodes(session: Session) -> None:
    """Places every unplaced node that is about to be inserted.

    This runs on every flush. Call sites still call `place_action` and
    `place_memory` directly, and they should. Placing a node at the call site
    happens before the surrounding code reads the row back, and the explicit
    call makes that ordering visible. This function is the fallback under those
    calls, so a fixture, script, test, or a call site added later still gets a
    branch without knowing the tree exists.

    Nodes whose adventure has not been inserted yet are skipped, because there
    is no id to attach a branch to. This case does not arise in practice, since
    an Action needs `adventure_id` before it can be written at all.

    The head is resolved once per adventure per flush and cached in `heads`.
    This saves more than a dictionary lookup. The identity map holds weak
    references, so a branch row that nothing else refers to is collected between
    two nodes and read from the database again for the next one. Resolving the
    head per node turned a fixture that wrote 200 actions in one flush into 200
    separate queries.
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
    """Recomputes the head depth after nodes are removed by undo or delete.

    A branch with no nodes of its own sits at its fork point, because that is
    the last node its story contains. The node is inherited from the parent, but
    it is still the tip. A root branch with no nodes has no story at all.
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
