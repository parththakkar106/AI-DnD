"""Retries, takes, and the attempts that pile up at one coordinate.

Retry first deleted the AI action and generated a replacement. A later version
kept the row and appended each attempt to a JSON list on it. Now every attempt is
its own node on the same branch at the same depth, and exactly one of them is
`live`. `app/attempts.py` owns the group and its invariants. The endpoints here
only query it.
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ... import attempts, limits, memorybank, models, schemas, tree
from ...context import cursors
from ...context import lineage
from ...database import get_db
from ...scripting import ScriptPipeline

from . import turns
from .deps import CurrentUser, get_adventure_or_404, router
from .nodes import delete_turn, last_action, stand_on
from .paging import action_window, annotate_takes, current_window
from .turns import SSE_HEADERS


@router.post("/{adventure_id}/retry")
def retry_action(
    adventure_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Regenerates the last AI action and keeps the discarded attempt.

    The attempt on screen stays as it was written. The shared script state and
    world state roll back to what the node before it left behind, and the new
    attempt is stored as a sibling at the same coordinate. No text the AI wrote
    is rewritten or deleted.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.rate_limit("turn", request, user)
    turns.check_demo_cap(db, user)
    turns.acquire_turn_lock(adventure_id)
    last_ai = None
    try:
        newest = last_action(adventure, db)
        if newest is not None and newest.type == "ai":
            last_ai = newest
            # Roll the state back to before this AI turn's hooks ran, so that
            # regenerating starts from a clean state rather than applying output
            # mutations on top of the attempt being replaced. If the preceding
            # node has no snapshot, which happens for a pre-SP4 row that the
            # migration could not derive one for, this call does nothing and
            # leaves the state as it is.
            attempts.roll_back_before(db, adventure, last_ai)
            db.commit()
            db.refresh(adventure)
    except BaseException:
        turns._active_turns.discard(adventure_id)
        raise
    return StreamingResponse(
        turns.with_turn_lock(
            adventure_id,
            turns.generate_turn(
                adventure, db, ScriptPipeline(adventure, db), user, retry_of=last_ai
            ),
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/{adventure_id}/actions/{action_id}/variants",
    response_model=list[schemas.VariantOut],
)
def list_variants(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Returns every attempt made for one AI turn.

    The client fetches these on demand, because the adventure payload carries
    only the counts. That keeps old narration out of every page load.

    You can address the turn by any of its attempts, not only the live one.
    Switching changes which row the story tells, and a client that holds an id it
    received a moment ago still has to be able to ask about the same turn.
    """
    get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    rows = attempts.group(db, action)
    if len(rows) < 2:
        return []  # Never retried, so the turn has one attempt.
    return [
        schemas.VariantOut(
            id=row.id,
            index=i,
            text=row.text,
            reasoning=row.reasoning,
            branch_id=row.branch_id,
            created_at=row.created_at.isoformat() if row.created_at else None,
            active=row.live,
        )
        for i, row in enumerate(rows)
    ]


@router.post(
    "/{adventure_id}/actions/{action_id}/variant", response_model=schemas.ActionOut
)
def select_variant(
    adventure_id: int,
    action_id: int,
    payload: schemas.VariantSelect,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Makes an earlier attempt live again and restores the state it produced.

    The restored state covers both the script state and the world state.

    Only the last action can be switched. The turns after an older action were
    written to continue the text that is currently active, so replacing that text
    would leave the story contradicting itself. The attempts of earlier turns
    stay readable through `list_variants`.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    rows = attempts.group(db, action)
    if not 0 <= payload.index < len(rows) or len(rows) < 2:
        raise HTTPException(400, "No such attempt for this action")
    newest = last_action(adventure, db)
    if newest is None or newest.depth != action.depth or newest.branch_id != action.branch_id:
        raise HTTPException(
            400,
            "Only the latest message can be switched — the story has already "
            "continued from this one.",
        )
    turns.acquire_turn_lock(adventure_id)
    try:
        chosen = rows[payload.index]
        if not chosen.live:
            # The story at this coordinate is about to change, so withdraw
            # anything derived from the previous text. A retry does the same
            # thing for the same reason.
            memorybank.forget_node(db, adventure, chosen)
            cursors.rewind_all(adventure, chosen.branch_id, (chosen.depth or 0) - 1)
        attempts.make_live(db, adventure, chosen)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(chosen)
        # Return the row that is now in the story, which is a different row
        # from the one the request addressed. An attempt is a node, so choosing
        # one moves the story onto it rather than rewriting a row.
        return chosen
    finally:
        turns._active_turns.discard(adventure_id)


@router.post(
    "/{adventure_id}/actions/{action_id}/fork", response_model=schemas.ActionPage
)
def fork_from_attempt(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Continues the story from this attempt, forking a branch if one is needed.

    There are three cases, and the first two do not fork:

    * The attempt is already the one the story tells, so there is nothing to do.
    * Its turn is the tip, so the attempts are still leaves that nothing was
      built on. The endpoint switches, as `/variant` does, and creates no branch.
    * The story has moved past its turn, so the endpoint forks. The attempt gets
      a branch of its own, and the line it leaves keeps every turn it has.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    # Check this before checking the shape of the turn, because a fork leaves
    # the promoted attempt alone on its branch. A client that repeats the call,
    # after a double click or a retried request, has to get the same answer
    # rather than an error saying the turn it just forked has nothing to fork
    # to.
    if action.live:
        # A live node already holds what its coordinate says, so there is no
        # attempt here to promote. On the path being read this call does
        # nothing, and it has to stay that way, so that a repeated call after a
        # double click or a retried request gets the same answer. Off the path
        # the node belongs to another line's story, and moving there is a branch
        # switch.
        #
        # The membership test covers the whole lineage, not `head_branch_id`. A
        # head borrows its ancestors' turns, so a live node on an ancestor is
        # already being read. Forking it would move the live row off the parent
        # and promote a sibling in its place, which rewrites the story on a
        # branch nobody asked about and on this one, which borrows that depth.
        if lineage.path_of(db, adventure).contains(action):
            return current_window(db, adventure)
        raise HTTPException(
            400,
            "That take is already the story on another branch. Switch to that "
            "branch to read it.",
        )
    if len(attempts.group(db, action)) < 2:
        raise HTTPException(
            400, "This turn has only one take, so there is nothing to fork to."
        )
    turns.acquire_turn_lock(adventure_id)
    try:
        stand_on(db, adventure, action)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(adventure)
        return current_window(db, adventure)
    finally:
        turns._active_turns.discard(adventure_id)


@router.post("/{adventure_id}/actions/{action_id}/takes")
def add_take(
    adventure_id: int,
    action_id: int,
    payload: schemas.TakeCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Plays a turn again, whoever wrote it.

    This endpoint replaces two earlier operations. `retry` gave an AI turn
    another attempt, but only for the newest turn, and a player's own message had
    no attempts at all, so changing text you had typed meant overwriting it and
    losing the story it led to. Here an AI turn regenerates, a player turn takes
    the text you supply, and neither depends on where in the story it sits.

    The tip is the only case that needs no branch, and only for an AI turn,
    because nothing was played after it and its attempts are still leaves. A
    player turn is never at the tip, since the reply to it is, so a player turn
    that has been answered always takes a branch.

    A branch is needed here for the same reason `fork` needs one. The turn being
    replayed already has a story after it, and that story was written as a
    continuation of the old text. `branch_at` leaves the path just before this
    turn, so the new attempt is written at the same depth under the same parent,
    and the line it leaves is unchanged. No node below is copied.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.rate_limit("turn", request, user)
    limits.check_row_cap("actions", db, user, adventure=adventure)
    turns.check_demo_cap(db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    if action.type not in ("do", "say", "story", "continue", "ai"):
        # The opening is not a turn anyone played, so it has no second attempt.
        # Editing the scenario is what changes it.
        raise HTTPException(400, "The opening of a story has no other take.")
    if action.depth is None or not lineage.path_of(db, adventure).contains(action):
        raise HTTPException(400, "That turn is not on the story you are reading.")
    turns.acquire_turn_lock(adventure_id)
    retry_of = None
    try:
        newest = last_action(adventure, db)
        at_the_tip = newest is not None and newest.id == action.id
        if at_the_tip and action.type == "ai":
            # Nothing was played after it, so its attempts are still leaves and
            # a branch would serve no purpose. This is the `retry` path.
            retry_of = action
            attempts.roll_back_before(db, adventure, action)
        else:
            # The turn has a story after it, written as a continuation of the
            # text that is there now. The new attempt leaves the path just
            # before the turn, so that story keeps the attempt it was written
            # for.
            tree.branch_at(db, adventure, action.depth - 1)
            attempts.roll_back_before(db, adventure, action)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(adventure)
    except BaseException:
        turns._active_turns.discard(adventure_id)
        raise
    if action.type == "ai":
        # There is no player action to write. The action this turn answers is
        # already on the path, borrowed from the line being left.
        stream = turns.generate_turn(
            adventure, db, ScriptPipeline(adventure, db), user, retry_of=retry_of
        )
    else:
        stream = turns.run_player_turn(
            adventure,
            db,
            schemas.ActionCreate(type=action.type, text=payload.text),
            user,
            # The client seeded its editor from the stored text, which already
            # carries the "> You ..." conventions.
            preformatted=True,
        )
    return StreamingResponse(
        turns.with_turn_lock(adventure_id, stream),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{adventure_id}/undo", response_model=schemas.ActionPage)
def undo_turn(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Deletes the last turn: the trailing AI action and its player action, if any.

    The endpoint also rolls the shared `script_state` back to before that turn
    ran, and it prunes any memory that summarized the removed actions. The turn
    lock prevents an undo while a turn is still generating.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    turns.acquire_turn_lock(adventure_id)
    try:
        # Only the last turn is removed, so fetch the two actions it can
        # consist of rather than the whole story.
        newest = (
            db.query(models.Action)
            .filter(
                models.Action.adventure_id == adventure.id,
                lineage.path_of(db, adventure).clause(models.Action),
            )
            .order_by(models.Action.depth.desc(), models.Action.id.desc())
            .limit(2)
            .all()
        )
        if not newest or newest[0].type == "start":
            raise HTTPException(400, "Nothing to undo")
        last = newest[0]
        before_that = newest[1] if len(newest) > 1 else None
        # Undo only what this branch owns. Everything before the fork is
        # borrowed from an ancestor and is part of that ancestor's story too, so
        # an undo here must never delete a turn out of another branch. The test
        # reads the row's own branch rather than the fork depth, because the
        # branch is what decides the case.
        if last.branch_id != adventure.head_branch_id:
            raise HTTPException(
                400, "Nothing to undo on this branch — the turns before it "
                     "belong to the branch it was forked from.",
            )
        first_removed = last
        if (last.type == "ai" and before_that is not None
                and before_that.type in ("do", "say", "story")
                and before_that.branch_id == adventure.head_branch_id):
            first_removed = before_that
        # The state the story returns to once the turn is gone, which is what
        # the node before the earliest removed one left behind. Read it before
        # the deletes, while those rows are still in the story.
        restore_to = attempts.preceding(db, adventure, first_removed)
        delete_turn(db, adventure, last)
        if first_removed is not last:
            delete_turn(db, adventure, first_removed)
        attempts.restore_state(adventure, restore_to)
        db.flush()  # Apply the deletes before anything reads the story back.
        db.expire(adventure, ["actions"])
        # The tip moves back with the deleted rows.
        tree.refresh_head(db, adventure)
        db.commit()
        db.refresh(adventure)
        # Return the newest window rather than the whole story. The client
        # replaces its transcript with this response, and the transcript is a
        # window. Returning everything would defeat the paging on the action a
        # player is most likely to repeat several times in a row.
        actions, total, has_more = action_window(db, adventure)
        return schemas.ActionPage(
            actions=[
            schemas.ActionOut.model_validate(a)
            for a in annotate_takes(db, adventure.id, actions)
        ],
            total=total,
            has_more=has_more,
        )
    finally:
        turns._active_turns.discard(adventure_id)
