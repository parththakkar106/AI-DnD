"""The story summary, as versions anchored on the story tree.

The summary used to be one text column on the adventure. That column was the
last piece of derived work that had no coordinate, and it behaved the way
everything without a coordinate behaves once a story can branch:

- Delete the turn whose summarizer run you did not like, and the text that run
  wrote stayed. `forget_node` withdrew the memories at that coordinate and
  rewound the cursors, and there was nothing about the summary for it to
  withdraw.
- Fork two turns back and the new line read the summary of the line it left,
  because both lines were the same row.
- Neither was recoverable afterwards. The rewrite is incremental: it hands the
  model the current text and asks for it to be updated, so once a bad version
  was stored, every later version was built on top of it.

A version here is a row with `(branch_id, depth)`, which is what a memory has
had since Phase 14. Reading the summary means taking the newest version on the
path being played, so all three fix themselves:

- Deleting a turn deletes the versions anchored on it, and the read falls
  through to the version before it. `forget_node` below does the deleting, from
  the same call sites that already withdraw memories.
- A fork inherits the versions written before the fork point and none written
  after it, because that is what the lineage clause selects. Nothing is copied.
- Every earlier version is still in the table, so falling back is a read rather
  than a repair.

Nothing prunes the table. See `models.Summary` for the arithmetic on that.

What lives where: this module owns the rows, meaning how a version is written,
which one is current, and what a deleted node takes with it. `app/memorybank.py`
owns the AI calls that produce the text, both the incremental pass that runs
after a turn and the full rebuild behind "Regenerate".
"""

from sqlalchemy.orm import Session, object_session

from . import models, tree
from .context import cursors, lineage


# ------------------------------------------------------------------ reading

def newest(db: Session, adventure: models.Adventure) -> models.Summary | None:
    """The current version, meaning the deepest one on the path being played.

    Ties break on id, so the newest row wins when a version the player typed and
    a version the pass wrote share a coordinate. That happens when the player
    edits the summary at the tip and the pass then folds in the turns since the
    last update: the edit is what the pass rewrote from, and the rewrite is what
    the story should read.
    """
    if adventure.id is None:
        return None
    return (
        db.query(models.Summary)
        .filter(
            models.Summary.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Summary),
        )
        .order_by(models.Summary.depth.desc(), models.Summary.id.desc())
        .first()
    )


def current(adventure: models.Adventure) -> str:
    """The current version's text, or "" when the story has no summary yet.

    This takes the adventure rather than a session, like `history.count`, so
    that `build_context` and `models.Adventure.story_summary` can call it with
    what they already hold. A detached adventure has no session to ask and
    answers "", which is the same answer an adventure with no versions gives.
    """
    db = object_session(adventure)
    if db is None:
        return ""
    row = newest(db, adventure)
    return row.text if row is not None else ""


# ------------------------------------------------------------------ writing

def _coordinate(
    db: Session, adventure: models.Adventure, node: models.Action | None
) -> tuple[int, int]:
    """Where a new version goes: the node's coordinate, or the head.

    A version the pass wrote names the last action it folded in. A version the
    player typed names no action, so it takes the head, which records the story
    they were reading while they typed. That is the rule `tree.place_memory`
    applies to a hand-written memory, and it is the reason a typed version stops
    following the reader onto branches whose story it does not describe.

    `head_branch` creates the root branch if the adventure has none. Every
    caller here is a write, so creating it is allowed, and a version with no
    branch would be invisible to every read.
    """
    if node is not None and node.branch_id is not None and node.depth is not None:
        return node.branch_id, node.depth
    return tree.head_branch(db, adventure).id, adventure.head_depth


def record(
    db: Session,
    adventure: models.Adventure,
    text: str,
    *,
    node: models.Action | None = None,
    source_start: int | None = None,
    hand_edited: bool = False,
) -> models.Summary:
    """Adds a version. The caller commits.

    `source_start` is the first depth this version folded in, and it is what
    `forget_node` rewinds the summary cursor to when the version is withdrawn.
    Leave it None for a version the player typed, which folded in nothing.
    """
    branch_id, depth = _coordinate(db, adventure, node)
    row = models.Summary(
        adventure_id=adventure.id,
        text=text,
        branch_id=branch_id,
        depth=depth,
        source_start=source_start,
        source_end=depth if source_start is not None else None,
        hand_edited=hand_edited,
    )
    db.add(row)
    return row


def set_text(db: Session, adventure: models.Adventure, text: str) -> models.Summary:
    """Stores what the player typed into the Story Summary field.

    The field saves on a debounce, so one editing session sends several
    requests. Each would otherwise become a version, and the table would fill up
    with the prefixes of a sentence. Instead an edit at a coordinate that
    already holds the current version rewrites that version in place, and one
    editing session produces one version.

    Rewriting in place keeps `source_start` if the version had one. The player
    is replacing the text of a version that folded in a stretch of story, not
    unclaiming the stretch: dropping the mark would leave the summary cursor
    past a stretch that nothing describes if the turn were later deleted.
    """
    branch_id, depth = _coordinate(db, adventure, None)
    row = newest(db, adventure)
    if row is not None and row.branch_id == branch_id and row.depth == depth:
        row.text = text
        row.hand_edited = True
        return row
    return record(db, adventure, text, hand_edited=True)


# ------------------------------------------------------------- withdrawing

def forget_node(
    db: Session, adventure: models.Adventure, action: models.Action
) -> int:
    """Withdraws the versions anchored on `action`, because it is being removed.

    `memorybank.forget_node` calls this, so every caller that already withdraws
    memories withdraws summaries too: deleting a turn, retrying one, and
    switching to another take of one.

    Withdrawing is the whole fix for the reported bug. The version the deleted
    turn produced goes, the version before it becomes current, and the summary
    cursor rewinds to where the withdrawn version started reading, so the
    stretch it covered is folded in again rather than counted as read.

    Only the summary cursor rewinds. The memory cursor is the caller's business,
    and the memories at this coordinate are withdrawn by their own pass.

    The opening node is the exception, and for the reason given in
    `memorybank.forget_node`: a version that folded in no stretch of story is one
    the player typed, so no deletion invalidates it, and at depth 0 it is
    typically the only version an adventure has.
    """
    if action.branch_id is None or action.depth is None:
        return 0
    doomed = (
        db.query(models.Summary)
        .filter(
            models.Summary.adventure_id == adventure.id,
            models.Summary.branch_id == action.branch_id,
            models.Summary.depth == action.depth,
        )
        .all()
    )
    if action.depth == lineage.ROOT_DEPTH:
        doomed = [s for s in doomed if s.source_start is not None]
    if not doomed:
        return 0
    starts = [s.source_start for s in doomed if s.source_start is not None]
    for row in doomed:
        db.delete(row)
    if starts:
        cursors.SUMMARY.rewind_to(adventure, action.branch_id, min(starts) - 1)
    return len(doomed)
