"""Reading, editing, and deleting individual actions.

`list_actions` pages through the current branch. The edit and delete endpoints
change one node, and deleting one removes the whole attempt group at its
coordinate through `nodes.delete_turn`.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ... import attempts, models, schemas, tree
from ...database import get_db

from . import turns
from .deps import CurrentUser, current_adventure, router
from .nodes import db_tip, delete_turn
from .paging import ACTION_PAGE, action_window, annotate_takes


@router.get("/{adventure_id}/actions", response_model=schemas.ActionPage)
def list_actions(
    before_id: int | None = None,
    limit: int = ACTION_PAGE,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Returns a page of the story, working backwards from the newest action.

    `before_id` is the oldest action the caller already holds, so scrolling up
    asks for what comes before it. Omit `before_id` for the newest window. See
    `action_window` for why this anchors on a row rather than an offset.
    """
    limit = max(1, min(limit, ACTION_PAGE * 4))
    actions, total, has_more = action_window(
        db, adventure, before_id=before_id, limit=limit
    )
    return schemas.ActionPage(
        actions=[
            schemas.ActionOut.model_validate(a)
            for a in annotate_takes(db, adventure.id, actions)
        ],
        total=total,
        has_more=has_more,
    )


@router.patch("/{adventure_id}/actions/{action_id}", response_model=schemas.ActionOut)
def update_action(
    adventure_id: int,
    action_id: int,
    payload: schemas.ActionUpdate,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    # One row holds one text. Nothing mirrors it now, so nothing else has to be
    # updated. The edit used to have to be written into the live variant entry
    # as well, or paging away and back reverted it.
    action.text = payload.text
    db.commit()
    return action


@router.delete("/{adventure_id}/actions/{action_id}", status_code=204)
def delete_action(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    # The lock is held for the same reason undo holds it: this endpoint puts
    # the shared state back, and a turn that is still generating is about to
    # write it.
    turns.acquire_turn_lock(adventure_id)
    try:
        # This works like undo. The turn is deleted with all of its attempts,
        # and whatever it produced is withdrawn. The marks are depths, and a
        # depth does not move when an action before it is deleted.
        delete_turn(db, adventure, action)
        db.flush()
        db.expire(adventure, ["actions"])
        # Deleting the newest action moves the tip. Deleting an action in the
        # middle leaves a gap in the depths, which is intended. See
        # `_backfill_tree`.
        tree.refresh_head(db, adventure)
        # The script state and the world state belong to the adventure, not to
        # the node, so deleting the node does not take back what it did to
        # them. Put them back to what the story now ends with, which is the
        # same restore a branch switch does.
        #
        # The world state carries the cooldown clock in `_meta.last_changed`,
        # and that clock is a depth. Leaving it set marked the deleted turn's
        # changes as having happened at the depth the next turn is played at,
        # so the referee refused them as changed too recently — on a turn the
        # story no longer contains. Deleting a middle action restores the tip's
        # own outcome, which is the state the adventure is already in.
        attempts.restore_state(adventure, db_tip(db, adventure))
        db.commit()
    finally:
        turns._active_turns.discard(adventure_id)
