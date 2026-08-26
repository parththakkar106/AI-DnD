"""Phase 14: tracks how far along a story the derived work has reached.

Two things are built from the story and stored beside it: the memories and the
story summary. Both need to record where they stopped.

That mark used to be a count, such as "the first 12 story actions are covered".
A count is a position in a list, and this list changes. If you delete an action
in front of the mark, every later action moves down one slot, so the mark now
covers an action it never read. The rules in `memorybank` for sliding cursors,
rewinding them, and converting between positions and `Action.index` all existed
to correct for that, and each rule was a chance to introduce a silent error.

A cursor here is an anchor instead. It stores `(branch_id, depth)`, naming the
node up to and including which the work is done. Deleting an action does not
move it, because a depth is a coordinate along a path rather than a position in
a list. The question "what is not covered yet" becomes
`history.count_after(anchor)`, which asks about the story rather than about a
list index, and it stays correct no matter what is deleted in front of it.

The branch half of the anchor is what makes it survive forking. A depth alone is
ambiguous once two branches both hold a node at depth 41. The anchor names the
branch, and `Path.depth_on` reads it back as a depth on whichever story is being
played. That read caps the depth at the fork, or reports nothing covered if the
anchor sits on a branch this path does not contain. Until forking ships there is
one branch, so this always returns the stored depth. That is the point: the
coordinate system is correct before anything depends on it.

`NO_DEPTH`, which is -1, means nothing is covered. A new adventure therefore
needs no special case, because every node is deeper than -1.
"""

from sqlalchemy.orm import Session

from .. import models
from . import history, lineage

NO_DEPTH = lineage.NO_DEPTH


class Cursor:
    """One anchor on the adventure row, for either the memory bank or the summary.

    The anchor is a pair of columns rather than a foreign key to the node. The
    node can be deleted, which is what undo does, and the boundary still means
    something afterwards. A foreign key would repeatedly fail to resolve.
    """

    def __init__(self, name: str):
        self.name = name
        self.branch_field = f"{name}_cursor_branch_id"
        self.depth_field = f"{name}_cursor_depth"

    # ------------------------------------------------------------- reading

    def stored(self, adventure: models.Adventure) -> tuple[int | None, int]:
        """Returns the anchor as written, without resolving it against a path."""
        depth = getattr(adventure, self.depth_field)
        return getattr(adventure, self.branch_field), (
            NO_DEPTH if depth is None else depth
        )

    def depth(self, db: Session, adventure: models.Adventure) -> int:
        """Returns the anchor as a depth on the story being played now."""
        branch_id, depth = self.stored(adventure)
        return lineage.path_of(db, adventure).depth_on(branch_id, depth)

    # ------------------------------------------------------------- writing

    def anchor(
        self, adventure: models.Adventure, branch_id: int | None, depth: int
    ) -> None:
        """Sets the anchor to a coordinate supplied directly.

        This is the plain setter beneath `anchor_at`. Only an import supplies a
        coordinate with no node to read it from. A v2 bundle carries the anchor
        itself, as described in `app/bundle.py`, and the node it named lives in
        a different database.
        """
        setattr(adventure, self.branch_field, branch_id)
        setattr(adventure, self.depth_field, max(depth, NO_DEPTH))

    def anchor_at(self, adventure: models.Adventure, node: models.Action) -> None:
        """Marks the work done up to and including `node`.

        This uses the node's own branch rather than the adventure's head. A block
        of six actions can end before the fork that created the current branch,
        and the coverage belongs where those actions are.
        """
        self.anchor(adventure, node.branch_id, lineage.NO_DEPTH
                    if node.depth is None else node.depth)

    def clear(self, adventure: models.Adventure) -> None:
        """Clears the anchor, so that nothing counts as covered.

        Call this when the branch the anchor referred to is deleted. On Postgres
        a stale branch id would never resolve. On SQLite the next fork can reuse
        an id that was just freed, and a stale anchor would then resolve onto a
        branch it never saw and report that stretch of story as summarized.
        Clearing the anchor costs one re-summarize, which is the safe direction
        to be wrong in.
        """
        self.anchor(adventure, None, NO_DEPTH)

    def rewind_to(
        self, adventure: models.Adventure, branch_id: int | None, depth: int
    ) -> None:
        """Moves the anchor back to `depth` if it is past that depth.

        The anchor never moves forward here. Moving backward is the only
        direction that is safe without knowing what else changed. Covering
        ground twice costs one summarizer call. Skipping ground removes a
        stretch of story from the memories permanently.
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
    """Returns a stretch of story to both the memory pass and the summary pass.

    The two move together because they cover the same actions from different
    directions. The summary folds in the memories, so withdrawing a memory
    without rewinding the summary would leave the summary claiming to have read
    something that no longer exists.
    """
    for cursor in ALL:
        cursor.rewind_to(adventure, branch_id, depth)


def anchor_at_position(
    adventure: models.Adventure, cursor: Cursor, position: int
) -> None:
    """Sets `cursor` from a count of covered story actions.

    A v1 bundle stores its mark as a count, and so does a database written
    before the anchors existed.

    The action at `position` in depth order is the node that carries the same
    meaning, and it keeps that meaning after something in front of it is
    deleted. A position past the end of the story is not an invalid value. An
    adventure that was fully caught up under the old rule can hold one, and it
    means the same thing as the tip, so this function anchors at the tip.

    `migrations._backfill_cursor_anchors` implements this rule in SQL for every
    adventure at once, without loading any of them. The two must agree.
    """
    if position <= 0:
        return
    covered = history.slice_(adventure, position - 1, 1) or history.tail(adventure, 1)
    if covered:
        cursor.anchor_at(adventure, covered[0])


def position_of(adventure: models.Adventure, depth: int) -> int:
    """Returns how many story actions lie at or before `depth`.

    This reads an anchor back as a count. The v1 export bundle stores cursors as
    counts, and builds that have never used depths read v1 bundles. This
    function is the only remaining code that speaks that coordinate system. The
    v2 format introduced in SP6 replaces it.
    """
    return max(history.count(adventure) - history.count_after(adventure, depth), 0)
