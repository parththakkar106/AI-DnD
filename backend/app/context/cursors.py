"""Phase 14 — how far along a story the derived work has got.

Two things are built from the story and stored beside it: the memories, and the
Story Summary. Both need to know where they left off, and that mark used to be
a *count* — "the first 12 story actions are covered". A count is a position in
a list, and this list moves: delete an action from in front of the mark and
every later action slides down a slot, so the mark now covers one it has never
seen. Every rule in `memorybank` about sliding cursors, rewinding them and
translating between positions and `Action.index` existed to patch that up, and
each was a separate chance to get it wrong in a way nothing reports.

A cursor here is an **anchor**: `(branch_id, depth)`, the node up to and
including which the work is done. Deleting an action does not move it, because
a depth is not a position — it is a coordinate along a path. "What is not
covered yet" becomes `history.count_after(anchor)`, which is a question about
the story rather than about a list index, and it answers correctly whatever has
been deleted from in front of it.

The branch half is what makes it survive forking. A depth alone is ambiguous
once two branches have a node 41; the anchor says which one, and
`Path.depth_on` reads it back as a depth on whatever story is being played —
capped at the fork, or "nothing covered" if the anchor sits on ground this path
never travelled. Until forking ships there is one branch and that is always a
no-op, which is the point: the coordinate system is right before anything needs
it to be.

`NO_DEPTH` (-1) is "nothing covered", so a fresh adventure needs no special
case: every node is deeper than -1.
"""

from sqlalchemy.orm import Session

from .. import models
from . import history, lineage

NO_DEPTH = lineage.NO_DEPTH


class Cursor:
    """One anchor on the adventure row: the memory bank's, or the summary's.

    A pair of columns rather than a foreign key to the node. The node can be
    deleted — that is most of what undo does — and the boundary is still
    meaningful afterwards, so a pointer that has to resolve would be a pointer
    that keeps not resolving.
    """

    def __init__(self, name: str):
        self.name = name
        self.branch_field = f"{name}_cursor_branch_id"
        self.depth_field = f"{name}_cursor_depth"

    # ------------------------------------------------------------- reading

    def stored(self, adventure: models.Adventure) -> tuple[int | None, int]:
        """The anchor exactly as written, unread by any path."""
        depth = getattr(adventure, self.depth_field)
        return getattr(adventure, self.branch_field), (
            NO_DEPTH if depth is None else depth
        )

    def depth(self, db: Session, adventure: models.Adventure) -> int:
        """The anchor as a depth on the story currently being played."""
        branch_id, depth = self.stored(adventure)
        return lineage.path_of(db, adventure).depth_on(branch_id, depth)

    # ------------------------------------------------------------- writing

    def anchor(
        self, adventure: models.Adventure, branch_id: int | None, depth: int
    ) -> None:
        """Put the anchor at a coordinate given outright.

        The plain setter under `anchor_at`. Only an import has a coordinate
        without a node to read it off — a v2 bundle carries the anchor itself
        (`app/bundle.py`), and the node it named lives in another database.
        """
        setattr(adventure, self.branch_field, branch_id)
        setattr(adventure, self.depth_field, max(depth, NO_DEPTH))

    def anchor_at(self, adventure: models.Adventure, node: models.Action) -> None:
        """Mark the work done up to and including `node`.

        Takes the node's own branch, not the adventure's head: a block of six
        actions can end before the fork this branch was made at, and the
        coverage belongs where the ground is.
        """
        self.anchor(adventure, node.branch_id, lineage.NO_DEPTH
                    if node.depth is None else node.depth)

    def rewind_to(
        self, adventure: models.Adventure, branch_id: int | None, depth: int
    ) -> None:
        """Move the anchor back to `depth` if it is past it; never forward.

        The one direction that is safe without knowing what else has happened:
        re-covering ground costs a summarizer call, skipping it loses a stretch
        of story out of the memories for good.
        """
        _, current = self.stored(adventure)
        if current <= depth:
            return
        setattr(adventure, self.branch_field, branch_id)
        setattr(adventure, self.depth_field, max(depth, NO_DEPTH))


MEMORY = Cursor("memory")
SUMMARY = Cursor("summary")
ALL = (MEMORY, SUMMARY)


def rewind_all(
    adventure: models.Adventure, branch_id: int | None, depth: int
) -> None:
    """Hand a stretch of story back to *both* passes.

    They move together because they cover the same ground from different sides:
    the summary folds in the memories, so a memory withdrawn without rewinding
    the summary leaves the summary claiming to have read something no longer
    there.
    """
    for cursor in ALL:
        cursor.rewind_to(adventure, branch_id, depth)


def anchor_at_position(
    adventure: models.Adventure, cursor: Cursor, position: int
) -> None:
    """Set `cursor` from a count of covered story actions — a v1 bundle's mark,
    or a database written before the anchors existed.

    The position-th story action in depth order is the node that says the same
    thing, and goes on saying it once something in front of it is deleted. A
    position past the end of the story is not a bad value: an adventure caught
    up under the older rule can carry one, and it means the same thing the tip
    does, so that is where it lands.

    The SQL half of this rule is `migrations._backfill_cursor_anchors`, which
    has to do it for every adventure at once without loading any of them; the
    two must agree.
    """
    if position <= 0:
        return
    covered = history.slice_(adventure, position - 1, 1) or history.tail(adventure, 1)
    if covered:
        cursor.anchor_at(adventure, covered[0])


def position_of(adventure: models.Adventure, depth: int) -> int:
    """How many story actions lie at or before `depth` — an anchor read back as
    a count.

    The v1 export bundle stores the cursors as positions, and a v1 bundle is
    read by builds that have never heard of a depth. This is the one place that
    still speaks that coordinate system, and SP6's v2 format retires it.
    """
    return max(history.count(adventure) - history.count_after(adventure, depth), 0)
