"""Phase 14 — which nodes are "this story".

`tree.py` decides where a node is written. This module is the other half: it
decides which nodes a read can see, and it is the **only** place that knows.

A branch owns the nodes played on it and *borrows* everything before its fork
point from its ancestors, so "the story on branch C" is not a column you can
filter on — it is an OR of ranges::

    (branch_id = C)                 -- C's own nodes, to the tip
    OR (branch_id = B AND depth <= 5)
    OR (branch_id = A AND depth <= 3)

which is exactly what `branches.lineage` spells out, newest first, computed
once when the fork happens. Reads never walk parent pointers to rebuild it.

Two properties fall out of the shape, and both are load-bearing:

* **The ranges are disjoint and descending.** A branch's own nodes always sit
  deeper than its fork point, and each lineage entry is capped at the fork
  depth of the branch beneath it. So ordering the whole clause by `depth`
  descending is the same as reading entry 0's nodes, then entry 1's, then
  entry 2's — which is what lets a tail read use only the newest few entries
  and stop.
* **Clause count is bounded by the context window, not by fork count.** A
  200-fork story whose newest branch is 40 turns long reads with one clause,
  because the window is covered before the second entry is reached. That is
  `prefix_covering`, and it is why `history.window_covering` can keep its shape.

Everything here is a read. Nothing in this module creates a branch or writes a
row: an adventure with no branch has no story, and healing that is the write
side's job (`tree.place_action`, and the flush guard in `models.py` behind it).
"""

from sqlalchemy import and_, false, or_
from sqlalchemy.orm import Session

from .. import models

# The depth of an adventure with no actions. Mirrors tree.NO_DEPTH; kept
# separately so a read never has to import the write half.
NO_DEPTH = -1

# The opening node of an adventure. Depth 0 exists only on the root branch — a
# fork starts its own nodes after the depth it forked at — so this names one
# node per adventure, not one per branch. It is also where migration 62 parked
# every memory written before memories had coordinates, which is why the two
# places that can retire a memory (`memorybank.forget_node`, and a v1 import
# with no depth to read) both have to say something about it.
ROOT_DEPTH = 0


def entries_of(branch: models.Branch) -> list[tuple[int, int | None]]:
    """`branch.lineage` as (branch_id, max_depth) pairs, newest first.

    An empty lineage reads as "this branch alone, to its tip" rather than as an
    error. That is what a root branch's lineage means, and it is what a branch
    row looks like in the moment between being inserted and having its own id
    to name — so the fallback is the truth, not a guess.
    """
    raw = branch.lineage if isinstance(branch.lineage, list) else []
    entries: list[tuple[int, int | None]] = []
    for item in raw:
        # JSON round-trips lists; a hand-written row might hold tuples.
        if not isinstance(item, (list, tuple)) or not item:
            continue
        branch_id = item[0]
        max_depth = item[1] if len(item) > 1 else None
        if not isinstance(branch_id, int):
            continue
        entries.append((branch_id, max_depth if isinstance(max_depth, int) else None))
    return entries or [(branch.id, None)]


class Path:
    """One story, as a clause and as a predicate.

    Holds the lineage entries newest first, plus the depth of the tip, which is
    only used to estimate how much story each entry covers.
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
        """The branch clause, over `model` (`Action` or `Memory`).

        `count` limits it to the newest `count` lineage entries — the windowed
        read. `None` is the whole lineage, which is what anything counting from
        the *oldest* end (a slice, a total) has to use.

        Every row this reads has a depth. Memories used to be the exception —
        a hand-written one had a branch and no depth, and needed an escape
        clause here to survive being capped at a fork. SP7 anchors them at the
        head instead (`tree.place_memory`), which is a better answer to the same
        problem: the memory is not exempt from the path, it is *on* one. A row
        with no depth is now a pre-tree leftover that no read should see.

        Actions also have to be *live* (SP4). A coordinate can hold several
        attempts at the same turn, and the story tells one of them; the losing
        siblings sit at the same branch and depth and are excluded here, once,
        so that no read of the story has to know that retries exist. Only
        `app/attempts.py` looks past this.

        An empty path yields `false`, not "no filter": an adventure whose nodes
        carry no branch has no story, and the loud version of that is an empty
        page, not every branch at once.
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
        """The Python half of `clause()` — keep the two in step.

        Used where the rows are already in memory (the scripting pipeline hands
        user scripts the whole history), so an already-loaded collection can be
        cut down to the path without a second read.

        `live` is checked first, and only on rows that have the attribute:
        memories have no siblings to lose to.
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
        """Oldest-first ordering. `depth` is the ordering key; `id` breaks the
        tie a pre-tree row (depth NULL) or a future sibling pair would leave."""
        return (node.depth if node.depth is not None else NO_DEPTH, node.id or 0)

    # ------------------------------------------------------------ windowing

    def prefix_covering(self, rows: int) -> int:
        """How many lineage entries it takes to hold the newest `rows` nodes.

        An estimate from depth arithmetic, not a query: entry *i* covers the
        depths between its own cap and the cap of the entry below it, and there
        is at most one node per depth on a path. So the count it returns is
        never too many, and is too few only where the story has gaps — an
        action deleted from the middle. The caller widens to the full lineage
        if the window comes up short, which costs a second query on a story
        somebody has deleted from, and nothing at all otherwise.
        """
        total = len(self.entries)
        if rows <= 0 or total == 0:
            return total
        covered = 0
        for i, (_, max_depth) in enumerate(self.entries):
            top = self.tip if max_depth is None else max_depth
            below = self.entries[i + 1][1] if i + 1 < total else NO_DEPTH
            if top is None or below is None:
                # No tip recorded, or a cap missing from a hand-written row:
                # nothing to estimate from, so read the lot rather than guess
                # short and hide the older half of the story.
                return total
            covered += max(top - below, 0)
            if covered >= rows:
                return i + 1
        return total

    def covering_after(self, depth: int) -> int:
        """How many lineage entries can hold a node deeper than `depth`.

        The counterpart to `prefix_covering`, and unlike it this is exact
        rather than an estimate: entry *i* holds nothing deeper than its own
        cap, and the caps descend, so the first entry capped at or below
        `depth` ends the search — it and everything older is behind the
        boundary. Reading "the story after the cursor" therefore names one
        branch on any story whose cursor is on its newest branch, however
        often it has forked.
        """
        for i, (_, max_depth) in enumerate(self.entries):
            if max_depth is not None and max_depth <= depth:
                return i
        return len(self.entries)

    def depth_on(self, branch_id: int | None, depth: int) -> int:
        """A stored `(branch_id, depth)` anchor, read as a depth on *this* path.

        An anchor is how far along a story some derived work has got — which
        memories cover, what the summary has folded in. It names a node, so
        moving to another path has to be answered rather than assumed:

        * the anchor's branch is on this path — the depth stands, capped at the
          fork the path takes off that branch, because nothing past the fork is
          on this story;
        * the branch is not on this path at all — the work was done on ground
          this story never travelled, so nothing here is covered.

        The second case cannot arise while an adventure has one branch: the
        anchor is always set from a node on it. It exists because the fallback
        for "I don't know" must be to redo the work, not to skip it.
        """
        if depth <= NO_DEPTH:
            return NO_DEPTH
        if branch_id is None:
            # A pre-tree anchor, or one set by hand. There is one story, so the
            # depth is a position in it and means what it says.
            return depth
        for entry_branch, max_depth in self.entries:
            if entry_branch == branch_id:
                return depth if max_depth is None else min(depth, max_depth)
        return NO_DEPTH


def branch_of(db: Session, adventure: models.Adventure) -> models.Branch | None:
    """The branch this adventure is being read at, or None if it has none.

    Deliberately not `tree.head_branch`, which creates one: a GET must not
    write. An adventure with no branch row also has no nodes carrying a branch,
    so the two agree — both say "no story here".
    """
    if adventure.head_branch_id is not None:
        branch = db.get(models.Branch, adventure.head_branch_id)
        if branch is not None:
            return branch
        # A head naming a branch that is gone: fall through to the root, the
        # same recovery `tree.head_branch` makes on the write side.
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
    """The story the adventure's head is currently on."""
    branch = branch_of(db, adventure)
    if branch is None:
        return Path([], adventure.head_depth)
    return Path(entries_of(branch), adventure.head_depth)
