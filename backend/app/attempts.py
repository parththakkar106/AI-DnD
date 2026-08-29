"""Phase 14, SP4: the attempts at one turn.

A retry used to rewrite the AI action in place and append the discarded attempt
to a JSON list on the same row. Seven separate bugs came from that arrangement.
The row's `text` duplicated one entry of a repeating group, a second column
duplicated its length, and every reader that touched the story during a retry
had to be told to ignore the row.

Now an attempt is a node. A retry writes a sibling at the same `(branch_id,
depth)` and marks it live. The previous attempt stays as it was written, at the
same coordinate, with `live = False`. Nothing is duplicated, so nothing can
diverge.

Two invariants hold the arrangement together, and this module is the only place
that maintains either one:

* Exactly one sibling in a group is live. `lineage.Path.clause` selects on it, so
  the other attempts are invisible to every read of the story, and none of those
  reads has to know that attempts exist.
* The assembled prompt is stored once per turn, on the live sibling. A
  `context_snapshot` is about 163 kB of prompt that every attempt at a turn
  shares, plus a few hundred bytes that differ, listed in `ATTEMPT_KEYS`. Giving
  each sibling its own copy would make a retry a permanent multiplier on the
  largest column in the database, which is what the JSON list was invented to
  avoid. The prompt therefore moves with the live flag, and a superseded sibling
  keeps only its own slices.

Ordering inside a group comes from `id`, not from `created_at`. Two attempts made
in the same second still have to page in the order they were made, and `id`
increases with every insert. SP8 dropped `variant_index`, an explicit ordinal
that carried the same order, once a run of the suite confirmed that the two
agreed in every group.
"""

import copy

from sqlalchemy.orm import Session, undefer

from . import models
from .context import lineage

# The slices of a context snapshot that belong to one attempt rather than to the
# turn. They are the world-state delta the attempt proposed and what the engine
# did with it, the script report, the model's literal reply, and the endpoint's
# token accounting. Each attempt is its own API call, and a retry is the call
# most likely to read the prompt back out of cache. Everything else in a snapshot
# is the prompt, which is assembled once per turn.
ATTEMPT_KEYS = ("world_state", "script", "raw_output", "usage")


# ------------------------------------------------------------------ reading

def group(db: Session, action: models.Action) -> list[models.Action]:
    """Returns every attempt at `action`'s turn, oldest first.

    The query keys on the parent rather than on the coordinate (SP9). The two
    agree until an attempt is forked onto its own branch. That attempt keeps its
    parent but leaves the `(branch, depth)` its siblings are still at, so a
    coordinate would report it as the only attempt at its turn, showing `1/1`
    where the player should see `1/3`.

    The parent also nests groups correctly without extra work. Attempts under C1
    and attempts under C2 share a depth, and until one of them forks they share a
    branch. Only the parent separates them, which is what makes a pager under C2
    read `2/2` rather than count C1's three as well.

    There are two fallbacks, and both mean the row predates the key being asked
    about. A node with no branch is a pre-tree row that no path contains, and a
    node with no parent is a pre-SP9 row the backfill could not place. Under the
    rule each was written with, both are the only attempt at their turn.
    """
    if action.branch_id is None or action.depth is None:
        return [action]
    if action.parent_id is None:
        # This row is pre-SP9, and the coordinate is the key those rows were
        # written under. A root node also reaches this branch and is genuinely
        # alone, because nothing is an attempt at the opening of a story.
        return (
            db.query(models.Action)
            .filter(
                models.Action.adventure_id == action.adventure_id,
                models.Action.branch_id == action.branch_id,
                models.Action.depth == action.depth,
                models.Action.parent_id.is_(None),
            )
            .order_by(models.Action.id)
            .all()
        )
    return (
        db.query(models.Action)
        .filter(
            models.Action.adventure_id == action.adventure_id,
            models.Action.parent_id == action.parent_id,
        )
        .order_by(models.Action.id)
        .all()
    )


def on_branch(rows: list[models.Action], node: models.Action) -> list[models.Action]:
    """Returns the attempts in `rows` that are on `node`'s own branch.

    `group` reports which attempts belong to this turn, and since SP9 that spans
    branches. An attempt forked onto its own line is still an attempt at the same
    turn, which is the reason for keying on the parent.

    Deletion is the one caller that must not follow a group across branches. An
    attempt on another branch is reachable through that branch and belongs to the
    story someone is telling there. Removing it because a turn was undone here
    would delete a line nobody asked about. The same parent and the same branch
    together are the coordinate, which is what every attempt at this turn meant
    before a fork could move one out of it.
    """
    return [row for row in rows if row.branch_id == node.branch_id]


def live_in(rows: list[models.Action]) -> models.Action | None:
    for row in rows:
        if row.live:
            return row
    return None


def preceding(
    db: Session, adventure: models.Adventure, node: models.Action
) -> models.Action | None:
    """Returns the node the story tells immediately before `node`.

    This reads "before this turn" as a fact about the path rather than as a
    snapshot taken from inside the turn, which is what makes the after-snapshots
    sufficient on their own. The query undefers both of them, because the only
    reason to fetch this row is to restore what it left behind.
    """
    if node.depth is None:
        return None
    return (
        db.query(models.Action)
        .filter(
            models.Action.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Action),
            models.Action.depth < node.depth,
        )
        .options(
            undefer(models.Action.state_after),
            undefer(models.Action.world_state_after),
        )
        .order_by(models.Action.depth.desc(), models.Action.id.desc())
        .first()
    )


# ------------------------------------------------------------------ writing

def restore_state(adventure: models.Adventure, node: models.Action | None) -> None:
    """Restores the script state and world state that `node` left behind.

    A NULL snapshot means leave the live state as it is, never reset it. Rows
    written before SP4 that the migration could not derive an outcome for carry
    NULLs, and overwriting a running adventure's state with an empty dict would
    be worse than doing nothing.
    """
    if node is None:
        return
    if isinstance(node.state_after, dict):
        adventure.script_state = copy.deepcopy(node.state_after)
    if isinstance(node.world_state_after, dict):
        adventure.world_state = copy.deepcopy(node.world_state_after)


def snapshot_outcome(adventure: models.Adventure, node: models.Action) -> None:
    """Records on `node` the state of the adventure now that the node has played."""
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    world = adventure.world_state if isinstance(adventure.world_state, dict) else {}
    node.state_after = copy.deepcopy(state)
    node.world_state_after = copy.deepcopy(world)


def roll_back_before(
    db: Session, adventure: models.Adventure, node: models.Action
) -> None:
    """Rewinds the shared state to what it was before `node` was played."""
    restore_state(adventure, preceding(db, adventure, node))


def add_attempt(
    db: Session,
    adventure: models.Adventure,
    previous: models.Action,
    replacement: models.Action,
) -> None:
    """Places `replacement` next to `previous` as the newer attempt at that turn.

    The placement is done here rather than through `tree.place_action`, which
    moves the head. A sibling is not a new turn. It is another attempt at the
    turn the head is already on.
    """
    replacement.branch_id = previous.branch_id
    replacement.depth = previous.depth
    # Copy the parent rather than resolve it from the path. An attempt belongs
    # to the turn it is an attempt at, and that is what `group` keys on.
    # Resolving it here would ask which node is live one depth back. That is the
    # same node right now, and it stops being the same node once the story forks
    # away from this turn.
    replacement.parent_id = previous.parent_id
    replacement.live = True
    # The replacement takes its place at the end of the group, because `group`
    # orders by `id` and this row has no id yet. Switching a three-attempt turn
    # back to attempt 1 and retrying therefore still pages 1, 2, 3, 4, which is
    # the order the attempts were made in.
    previous.live = False
    # The replacement was assembled with a fresh snapshot, so the prompt for
    # this turn is now the one it carries. The superseded attempt keeps only the
    # slices that were its own.
    keep_own_slices(previous)


def make_live(
    db: Session, adventure: models.Adventure, node: models.Action
) -> list[models.Action]:
    """Makes `node` the attempt the story tells, and restores its outcome.

    Returns the group, so that a caller reporting on it does not read it twice.
    """
    rows = group(db, node)
    previous = live_in(rows)
    if previous is not None and previous is not node:
        hand_over_the_prompt(previous, node)
    for row in rows:
        row.live = row is node
    restore_state(adventure, node)
    return rows


# ------------------------------------------------- the prompt, stored once

def keep_own_slices(node: models.Action) -> None:
    """Reduces `node`'s snapshot to the slices that are only its own."""
    snapshot = node.context_snapshot
    if not isinstance(snapshot, dict):
        return
    node.context_snapshot = {
        key: snapshot[key] for key in ATTEMPT_KEYS if key in snapshot
    } or None


def hand_over_the_prompt(giver: models.Action, taker: models.Action) -> None:
    """Moves the turn's assembled prompt from one attempt to another.

    The caller runs this when the live flag moves, so that the row in the story
    is always the row the Insights viewer can explain. Nothing is copied. The
    prompt exists once before and once after, on whichever sibling is being read.
    """
    held = giver.context_snapshot if isinstance(giver.context_snapshot, dict) else {}
    shared = {k: v for k, v in held.items() if k not in ATTEMPT_KEYS}
    if not shared:
        return
    keep_own_slices(giver)
    own = taker.context_snapshot if isinstance(taker.context_snapshot, dict) else {}
    taker.context_snapshot = shared | {
        k: v for k, v in own.items() if k in ATTEMPT_KEYS
    }
