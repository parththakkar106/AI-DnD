"""Phase 14: decides which nodes make up one story.

`tree.py` decides where a node is written. This module decides which nodes a
read can see, and it is the only place that makes that decision.

A branch owns the nodes played on it and inherits everything before its fork
point from its ancestors. "The story on branch C" is therefore not a value you
can filter a column on. It is an OR of ranges::

    (branch_id = C)                 -- C's own nodes, through to the tip
    OR (branch_id = B AND depth <= 5)
    OR (branch_id = A AND depth <= 3)

The `branches.lineage` column records exactly that list, newest first. The fork
computes it once, so no read walks parent pointers to rebuild it.

The shape of the list gives two properties that the windowed reads depend on:

- The ranges do not overlap, and they descend. A branch's own nodes always sit
  deeper than its fork point, and each lineage entry is capped at the fork depth
  of the branch below it. Ordering the whole clause by `depth` descending
  therefore reads entry 0's nodes, then entry 1's, then entry 2's. A tail read
  can use the newest few entries and stop.
- The number of clauses depends on the size of the context window, not on how
  many times the story has forked. A story with 200 forks whose newest branch
  runs 40 turns reads with a single clause, because the window is covered before
  the second entry is reached. `prefix_covering` implements this, and it is why
  `history.window_covering` can keep its current shape.

Everything in this module reads. Nothing here creates a branch or writes a row.
An adventure with no branch has no story, and the write side repairs that. See
`tree.place_action` and the flush listener in `models.py`.
"""

from sqlalchemy import and_, false, or_
from sqlalchemy.orm import Session

from .. import models

# The depth of an adventure that has no actions. This mirrors `tree.NO_DEPTH`.
# It is duplicated here so that a read never has to import the write half.
NO_DEPTH = -1

# The opening node of an adventure. Depth 0 exists only on the root branch,
# because a fork starts its own nodes after the depth it forked at. This
# constant therefore names one node per adventure rather than one per branch.
#
# Migration 62 also placed every memory written before memories had coordinates
# at depth 0. That is why both places that can retire a memory must handle this
# depth: `memorybank.forget_node`, and a v1 import that has no depth to read.
ROOT_DEPTH = 0


def entries_of(branch: models.Branch) -> list[tuple[int, int | None]]:
    """Returns `branch.lineage` as (branch_id, max_depth) pairs, newest first.

    An empty lineage means this branch alone, through to its tip. That is not an
    error. It is what a root branch's lineage means, and it is also what a
    branch row holds between the moment it is inserted and the moment its own id
    is written into the column. The fallback is therefore correct rather than a
    guess.
    """
    raw = branch.lineage if isinstance(branch.lineage, list) else []
    entries: list[tuple[int, int | None]] = []
    for item in raw:
        # JSON round-trips lists, but a hand-written row might hold tuples.
        if not isinstance(item, (list, tuple)) or not item:
            continue
        branch_id = item[0]
        max_depth = item[1] if len(item) > 1 else None
        if not isinstance(branch_id, int):
            continue
        entries.append((branch_id, max_depth if isinstance(max_depth, int) else None))
    return entries or [(branch.id, None)]


class Path:
    """One story, expressed as a SQL clause and as a Python predicate.

    The object holds the lineage entries newest first, plus the depth of the
    tip. The tip is used only to estimate how much story each entry covers.
    """

    def __init__(self, entries: list[tuple[int, int | None]], tip: int | None = None):
        self.entries = entries
        self.tip = tip

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    # ---------------------------------------------------------------- SQL

    def clause(
        self,
        model=models.Action,
        count: int | None = None,
    ):
        """Returns the branch clause over `model`, which is `Action` or `Memory`.

        `count` limits the clause to the newest `count` lineage entries, which
        produces a windowed read. Pass `None` for the whole lineage. Any caller
        that counts from the oldest end, such as a slice or a total, must pass
        `None`.

        Every row this clause selects has a depth. Memories were once an
        exception, because a hand-written memory had a branch but no depth and
        needed an escape clause here to avoid being capped at a fork. SP7
        anchors those memories at the head instead. See `tree.place_memory`. A
        memory is now on a path rather than exempt from one, and a row with no
        depth is a pre-tree leftover that no read should return.

        Actions must also be live, as of SP4. One coordinate can hold several
        attempts at a turn, and the story uses one of them. The other attempts
        sit at the same branch and depth, and this clause excludes them once, so
        that no read of the story has to account for retries. Only
        `app/attempts.py` looks past this filter.

        An empty path returns `false` rather than no filter at all. An adventure
        whose nodes carry no branch has no story, and the correct way to show
        that is an empty page rather than every branch at once.
        """
        entries = self.entries if count is None else self.entries[:count]
        if not entries:
            return false()
        on_path = or_(*[self._entry_clause(model, b, d) for b, d in entries])
        if model is models.Action:
            return and_(on_path, models.Action.live.is_(True))
        return on_path

    @staticmethod
    def _entry_clause(model, branch_id: int, max_depth: int | None):
        if max_depth is None:
            return model.branch_id == branch_id
        return and_(model.branch_id == branch_id, model.depth <= max_depth)

    # ------------------------------------------------------------- Python

    def contains(self, node) -> bool:
        """Returns whether `node` is on this path.

        This is the Python equivalent of `clause()`. Keep the two in step.

        Callers use it where the rows are already in memory. The scripting
        pipeline hands user scripts the whole history, so a loaded collection
        can be reduced to the path without a second query.

        The method checks `live` first, and only on rows that define the
        attribute, because memories have no siblings.
        """
        if getattr(node, "live", True) is False:
            return False
        for branch_id, max_depth in self.entries:
            if node.branch_id != branch_id:
                continue
            if max_depth is None:
                return True
            if node.depth is not None and node.depth <= max_depth:
                return True
        return False

    def sort_key(self, node) -> tuple[int, int]:
        """Returns a sort key that orders nodes from oldest to newest.

        `depth` is the ordering key. `id` breaks ties, which a pre-tree row with
        a NULL depth or a pair of siblings can produce.
        """
        return (node.depth if node.depth is not None else NO_DEPTH, node.id or 0)

    # ------------------------------------------------------------ windowing

    def prefix_covering(self, rows: int) -> int:
        """Returns how many lineage entries hold the newest `rows` nodes.

        The result is an estimate from depth arithmetic rather than a query.
        Entry *i* covers the depths between its own cap and the cap of the entry
        below it, and a path holds at most one node per depth. The estimate is
        therefore never too large. It is too small only when the story has gaps,
        which happens after an action is deleted from the middle. In that case
        the caller widens the read to the whole lineage, which costs one extra
        query.
        """
        total = len(self.entries)
        if rows <= 0 or total == 0:
            return total
        covered = 0
        for i, (_, max_depth) in enumerate(self.entries):
            top = self.tip if max_depth is None else max_depth
            below = self.entries[i + 1][1] if i + 1 < total else NO_DEPTH
            if top is None or below is None:
                # Either no tip was recorded, or a hand-written row is missing a
                # cap. There is nothing to estimate from, so return every entry.
                # Guessing low would hide the older half of the story.
                return total
            covered += max(top - below, 0)
            if covered >= rows:
                return i + 1
        return total

    def covering_after(self, depth: int) -> int:
        """Returns how many lineage entries can hold a node deeper than `depth`.

        This is the counterpart to `prefix_covering`, and it is exact rather than
        an estimate. Entry *i* holds nothing deeper than its own cap, and the
        caps descend, so the first entry capped at or below `depth` ends the
        search. That entry and every older one fall behind the boundary. As a
        result, reading the story after the cursor touches one branch on any
        story whose cursor sits on its newest branch, however many times the
        story has forked.
        """
        for i, (_, max_depth) in enumerate(self.entries):
            if max_depth is not None and max_depth <= depth:
                return i
        return len(self.entries)

    def depth_on(self, branch_id: int | None, depth: int) -> int:
        """Reads a stored `(branch_id, depth)` anchor as a depth on this path.

        An anchor records how far along a story some derived work reached, such
        as which actions the memories cover or what the summary folded in. The
        anchor names a node, so moving to a different path needs an explicit
        answer. There are two cases:

        - The anchor's branch is on this path. The depth stands, capped at the
          fork where this path leaves that branch, because nothing past the fork
          belongs to this story.
        - The anchor's branch is not on this path. The work was done on a branch
          this story does not contain, so nothing here counts as covered.

        The second case cannot occur while an adventure has one branch, because
        the anchor is always set from a node on it. It exists because the safe
        answer to an unknown anchor is to redo the work rather than skip it.
        """
        if depth <= NO_DEPTH:
            return NO_DEPTH
        if branch_id is None:
            # The anchor predates the tree, or someone set it by hand. There is
            # only one story, so the depth is a position in it.
            return depth
        for entry_branch, max_depth in self.entries:
            if entry_branch == branch_id:
                return depth if max_depth is None else min(depth, max_depth)
        return NO_DEPTH


def branch_of(db: Session, adventure: models.Adventure) -> models.Branch | None:
    """Returns the branch this adventure is read at, or None if it has none.

    This function is deliberately not `tree.head_branch`, which creates a branch.
    A GET request must not write. An adventure with no branch row also has no
    nodes that carry a branch, so both answers agree that there is no story.
    """
    if adventure.head_branch_id is not None:
        branch = db.get(models.Branch, adventure.head_branch_id)
        if branch is not None:
            return branch
        # The head points at a branch that no longer exists. Fall through to the
        # root, which is the same recovery that `tree.head_branch` performs on
        # the write side.
    return (
        db.query(models.Branch)
        .filter(
            models.Branch.adventure_id == adventure.id,
            models.Branch.parent_branch_id.is_(None),
        )
        .order_by(models.Branch.id)
        .first()
    )


def path_of(db: Session, adventure: models.Adventure) -> Path:
    """Returns the story that the adventure's head currently sits on."""
    branch = branch_of(db, adventure)
    if branch is None:
        return Path([], adventure.head_depth)
    return Path(entries_of(branch), adventure.head_depth)
