"""Phase 14, SP4 — the attempts at one turn.

A retry used to rewrite the AI action in place and push the discarded take into
a JSON list on the same row. That is where seven separate bugs came from: the
row's `text` mirrored one entry of a repeating group, `variant_count` mirrored
its length, and every reader that touched the story during a retry had to be
told to pretend the row was not there.

Now an attempt is a **node**. Retry writes a sibling at the same
`(branch_id, depth)` and marks it live; the previous one stays exactly as it
was written, at the same coordinate, `live = False`. Nothing is mirrored, so
nothing can drift.

Two invariants hold the arrangement together, and this module is the only place
that maintains either:

* **Exactly one sibling in a group is live.** `lineage.Path.clause` selects on
  it, so the losing attempts are invisible to every read of the story without
  any of those reads knowing that attempts exist.
* **The assembled prompt is stored once per turn, on the live sibling.**
  A `context_snapshot` is ~163 kB of prompt that every attempt at a turn
  shares, plus a few hundred bytes that differ (`ATTEMPT_KEYS`). Giving each
  sibling its own copy would have made retry a permanent multiplier on the
  biggest column in the database — the thing the JSON list was invented to
  avoid. So the prompt moves with the live flag, and a superseded sibling keeps
  only its own slices.

Ordering inside a group is `variant_index`, an explicit ordinal, not
`created_at`. Two attempts made in the same second must still page in the order
they were made, and the migration that split the old JSON lists had to be able
to state the order rather than reconstruct it.
"""

import copy

from sqlalchemy.orm import Session, undefer

from . import models
from .context import lineage

# The slices of a context snapshot that belong to one attempt rather than to
# the turn: the world-state delta it proposed and what the referee did with it,
# the script report, and the model's literal reply. Everything else in a
# snapshot is the prompt, which is assembled once per turn.
ATTEMPT_KEYS = ("world_state", "script", "raw_output")


# ------------------------------------------------------------------ reading

def group(db: Session, action: models.Action) -> list[models.Action]:
    """Every attempt at `action`'s turn, oldest first.

    Keyed on the **parent**, not on the coordinate (SP9). The two agree right up
    until a take is forked onto its own branch: it keeps its parent but leaves
    the (branch, depth) its siblings are still at, so a coordinate would report
    it as the only take of its turn — `1/1` where the player is owed `1/3`.

    The parent also gets the nesting right without being asked. Takes under C1
    and takes under C2 share a depth and, until one of them forks, a branch;
    only the parent separates them, which is what makes a pager under C2 read
    `2/2` instead of counting C1's three as well.

    Two fallbacks, both meaning "this row predates the key being asked about":
    a node with no branch is a pre-tree row no path contains, and a node with no
    parent is a pre-SP9 row the backfill could not place. Both are their own
    only attempt under the rule they were written with.
    """
    if action.branch_id is None or action.depth is None:
        return [action]
    if action.parent_id is None:
        # Pre-SP9, and the coordinate is the key those rows were written under.
        # Root nodes land here too and are genuinely alone: nothing is a take of
        # the opening of a story.
        return (
            db.query(models.Action)
            .filter(
                models.Action.adventure_id == action.adventure_id,
                models.Action.branch_id == action.branch_id,
                models.Action.depth == action.depth,
                models.Action.parent_id.is_(None),
            )
            .order_by(models.Action.variant_index, models.Action.id)
            .all()
        )
    return (
        db.query(models.Action)
        .filter(
            models.Action.adventure_id == action.adventure_id,
            models.Action.parent_id == action.parent_id,
        )
        .order_by(models.Action.variant_index, models.Action.id)
        .all()
    )


def on_branch(rows: list[models.Action], node: models.Action) -> list[models.Action]:
    """The takes in `rows` that sit on `node`'s own branch.

    `group` answers "which takes are of this turn", and since SP9 that spans
    branches — a take forked onto its own line is still a take of the same turn,
    which is the whole point of keying on the parent.

    Deleting is the one caller that must not follow it there. A take on another
    branch is reachable through that branch and belongs to the story somebody is
    telling on it; removing it because a turn was undone over here would delete
    a line nobody asked about. Same parent *and* same branch is the coordinate,
    which is what "every attempt at this turn" meant before a fork could move
    one out of it.
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
    """The node the story tells immediately before `node`.

    "Before this turn" as a fact about the path rather than as a snapshot taken
    from inside the turn — which is what makes the after-snapshots enough on
    their own. Undefers both of them because the only reason to ask for this
    row is to put back what it left behind.
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
    """Put back the script scoreboard and world state `node` left behind.

    A NULL snapshot means "leave the live state alone", never "reset it": rows
    written before SP4 that the migration could not derive an outcome for carry
    NULLs, and clobbering a running adventure's scoreboard with an empty dict
    would be a far worse answer than doing nothing.
    """
    if node is None:
        return
    if isinstance(node.state_after, dict):
        adventure.script_state = copy.deepcopy(node.state_after)
    if isinstance(node.world_state_after, dict):
        adventure.world_state = copy.deepcopy(node.world_state_after)


def snapshot_outcome(adventure: models.Adventure, node: models.Action) -> None:
    """Record on `node` what the adventure looks like now that it has played."""
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    world = adventure.world_state if isinstance(adventure.world_state, dict) else {}
    node.state_after = copy.deepcopy(state)
    node.world_state_after = copy.deepcopy(world)


def roll_back_before(
    db: Session, adventure: models.Adventure, node: models.Action
) -> None:
    """Rewind the shared state to before `node` was played."""
    restore_state(adventure, preceding(db, adventure, node))


def add_attempt(
    db: Session,
    adventure: models.Adventure,
    previous: models.Action,
    replacement: models.Action,
) -> None:
    """Put `replacement` beside `previous` as the newer attempt at that turn.

    Placed by hand rather than through `tree.place_action`, which would read the
    depth off the legacy `index` and move the head: a sibling is not a new turn,
    it is another take on the one the head is already standing on.
    """
    replacement.branch_id = previous.branch_id
    replacement.depth = previous.depth
    # Copied, never resolved from the path: a take belongs to the turn it is a
    # take *of*, and that is what `group` keys on. Resolving it here would ask
    # what is live one depth back, which is the same node right now and stops
    # being once this turn is forked away from.
    replacement.parent_id = previous.parent_id
    replacement.live = True
    # The end of the group, not one past `previous` — which is only the same
    # thing when `previous` is the newest take. Switch a three-take turn back to
    # take 1 and retry, and `previous.variant_index + 1` collides with take 2;
    # `renumber` then breaks the tie by id and files the new attempt *between*
    # takes 2 and 3, so the pager walks the takes in an order they were not made
    # in. `group` is oldest-first, and `replacement` is not in it yet.
    siblings = group(db, previous)
    replacement.variant_index = 1 + max(
        (s.variant_index for s in siblings if s.variant_index is not None),
        default=previous.variant_index or 0,
    )
    previous.live = False
    # The replacement was assembled with a fresh snapshot, so the prompt for
    # this turn is now the one it carries; the superseded attempt keeps only
    # what was its own.
    keep_own_slices(previous)


def make_live(
    db: Session, adventure: models.Adventure, node: models.Action
) -> list[models.Action]:
    """Make `node` the attempt the story tells, and put its outcome back.

    Returns the group, renumbered, so a caller that wants to report on it does
    not read it twice.
    """
    rows = group(db, node)
    previous = live_in(rows)
    if previous is not None and previous is not node:
        hand_over_the_prompt(previous, node)
    for row in rows:
        row.live = row is node
    restore_state(adventure, node)
    renumber(rows)
    return rows


def renumber(rows: list[models.Action]) -> None:
    """Refresh the group-shape cache the page response reads.

    `variant_count` is 0 rather than 1 for a turn nobody retried, because the
    pager's question is "is there anything to page through?" and the answer for
    a single attempt is no.
    """
    count = len(rows) if len(rows) > 1 else 0
    for i, row in enumerate(rows):
        row.variant_index = i
        row.variant_count = count


# ------------------------------------------------- the prompt, stored once

def keep_own_slices(node: models.Action) -> None:
    """Strip `node`'s snapshot back to what is only its own."""
    snapshot = node.context_snapshot
    if not isinstance(snapshot, dict):
        return
    node.context_snapshot = {
        key: snapshot[key] for key in ATTEMPT_KEYS if key in snapshot
    } or None


def hand_over_the_prompt(giver: models.Action, taker: models.Action) -> None:
    """Move the turn's assembled prompt from one attempt to another.

    Called when the live flag moves, so the row in the story is always the row
    the Insights viewer can explain. Nothing is copied — the prompt exists once
    before and once after, on whichever sibling is being read.
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
