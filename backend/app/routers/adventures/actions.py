"""Reading, editing, and deleting individual actions.

`list_actions` pages through the current branch. The edit and delete endpoints
change one node, and deleting one removes the whole attempt group at its
coordinate through `nodes.delete_turn`.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ... import models, schemas, tree
from ...database import get_db

from .deps import CurrentUser, get_adventure_or_404, router
from .nodes import delete_turn
from .paging import ACTION_PAGE, action_window, annotate_takes


@router.get("/{adventure_id}/actions", response_model=schemas.ActionPage)
def list_actions(
    adventure_id: int,
    before_id: int | None = None,
    limit: int = ACTION_PAGE,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Returns a page of the story, working backwards from the newest action.

    `before_id` is the oldest action the caller already holds, so scrolling up
    asks for what comes before it. Omit `before_id` for the newest window. See
    `action_window` for why this anchors on a row rather than an offset.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
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
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
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
    user: models.User = CurrentUser,
):
    adventure = get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    # This works like undo. The turn is deleted with all of its attempts, and
    # whatever it produced is withdrawn. Nothing else is needed, because the
    # marks are depths, and a depth does not move when an action before it is
    # deleted.
    delete_turn(db, adventure, action)
    db.flush()
    db.expire(adventure, ["actions"])
    # Deleting the newest action moves the tip. Deleting an action in the
    # middle leaves a gap in the depths, which is intended. See
    # `_backfill_tree`.
    tree.refresh_head(db, adventure)
    db.commit()
