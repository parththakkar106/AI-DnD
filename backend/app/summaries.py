"""The story summary, stored as versions anchored on the story tree.

The summary used to be one text column on the adventure. That column was the
last piece of derived work without a coordinate. Three defects followed from
that:

- Deleting a turn left the summary that turn produced in place. `forget_node`
  withdrew the memories at the turn's coordinate and rewound the cursors. It had
  no summary row to withdraw.
- Forking two turns back read the same text as the line the player left, because
  both lines shared one column.
- Neither defect was recoverable. The rewrite is incremental. It sends the model
  the current text and asks for an updated version. Once a bad summary was
  stored, every later summary was built from it.

A version here is a row with a `(branch_id, depth)` coordinate. A memory has had
one since Phase 14. A read takes the newest version on the path being played,
which corrects all three defects:

- Deleting a turn deletes the versions anchored on it. The read then returns the
  version before it. `forget_node` below performs the delete, from the same call
  sites that withdraw memories.
- A fork inherits the versions written before the fork point and none written
  after it, because that is what the lineage clause selects. No row is copied.
- Every earlier version stays in the table, so returning to one is a read rather
  than a repair.

Nothing prunes the table. For the size arithmetic, see `models.Summary`.

This module owns the rows. It defines how a version is written, which version is
current, and which versions a delete removes. `app/memorybank.py` owns the AI
calls that produce the text, both the incremental pass after a turn and the full
rebuild behind "Regenerate".
"""

from sqlalchemy.orm import Session, object_session

from . import models, tree
from .context import cursors, lineage


# ------------------------------------------------------------------ reading

def newest(db: Session, adventure: models.Adventure) -> models.Summary | None:
    """Returns the current version, the deepest one on the path being played.

    Ties break on id, so the newest row wins when two versions share a
    coordinate. Two versions share one when a player edits the summary at the
    tip and the pass then folds in the turns since the last update. The pass
    rewrites from the player's edit, and the story reads the rewrite.
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
    """Returns the current version's text, or "" if the story has no version.

    This takes the adventure rather than a session, as `history.count` does, so
    `build_context` and `models.Adventure.story_summary` can call it with the
    value they hold. A detached adventure has no session to query and returns
    "", which is also the answer for an adventure with no versions.
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
    """Returns where a new version goes: the node's coordinate, or the head.

    A version the pass writes names the last action it folded in. A version the
    player types names no action, so it takes the head. The head records the
    story the player read while typing. `tree.place_memory` applies the same rule
    to a hand-written memory. The rule stops a typed version from following the
    reader onto branches whose story it does not describe.

    If the adventure has no branch, `head_branch` creates the root branch. Every
    caller here writes, so creating a branch is allowed. A version with no branch
    is invisible to every read.
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

    `source_start` is the first depth this version folded in. When `forget_node`
    withdraws the version, it rewinds the summary cursor to that depth. Pass None
    for a version the player typed, which folds in no story.
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
    """Stores what the player types into the Story Summary field.

    The field saves on a debounce, so one editing session sends several requests.
    Each request would otherwise add a version, and the table would fill with the
    prefixes of a sentence. If the current version already sits at the head
    coordinate, this rewrites that version instead. One editing session then
    produces one version.

    A rewrite keeps `source_start` if the version has one. The player replaces
    the text of a version that folded in a stretch of story, and does not
    unclaim the stretch. If this cleared the mark and the turn were deleted
    later, the summary cursor would sit past a stretch that no version describes.
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
    """Withdraws the versions anchored on `action`, which is being removed.

    `memorybank.forget_node` calls this, so every call site that withdraws
    memories withdraws summaries too: deleting a turn, retrying one, and
    switching to another take of one.

    This withdrawal is the fix for the reported defect. The version the deleted
    turn produced goes, the version before it becomes current, and the summary
    cursor rewinds to the depth the withdrawn version started reading at. The
    pass then folds in that stretch again rather than counting it as read.

    Only the summary cursor rewinds. The caller owns the memory cursor, and the
    memory pass withdraws the memories at this coordinate.

    The opening node is the exception, for the reason `memorybank.forget_node`
    gives. A version that folded in no story is one the player typed, so no
    deletion invalidates it. At depth 0 it is usually the only version an
    adventure has.
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
