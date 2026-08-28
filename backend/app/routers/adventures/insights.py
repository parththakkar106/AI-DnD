"""Read-only views of the context an adventure would send, or did send.

The dry run assembles a prompt without calling the model. The per-action endpoint
returns the prompt a turn was actually generated from. Neither writes anything.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ... import auth, memorybank, models
from ...context import build_context
from ...database import get_db
from ..settings import get_settings

from .deps import CurrentUser, get_adventure_or_404, router


@router.get("/{adventure_id}/context")
async def dry_run_context(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Returns what the app would send to the AI if the player continued now."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    settings = get_settings(db, user)
    if auth.resolve_provider_config(settings).using_demo:
        memories = (
            {"used": [], "error": "Memory bank is unavailable on the shared demo key."}
            if adventure.memory_bank_enabled
            else None
        )
    else:
        memories = await memorybank.retrieve_memories(adventure, settings, update_stats=False)
    _, _, report = build_context(adventure, settings, memories)
    return report


@router.get("/{adventure_id}/actions/{action_id}/context")
def action_context(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    if action.context_snapshot is None:
        raise HTTPException(404, "No context snapshot for this action")
    return action.context_snapshot
