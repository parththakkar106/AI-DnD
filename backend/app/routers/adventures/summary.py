"""Rebuilding the story summary on demand.

This module holds no endpoint for reading or editing the summary. The summary
travels on the adventure as `story_summary`, and a player's edits arrive through
`PATCH /adventures/{id}`, which `crud.update_adventure` routes into
`summaries.set_text`. This module serves one button, which discards the current
version and reads the story again.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ... import auth, memorybank, models, schemas
from ...database import get_db
from ...providers import ProviderError
from ..settings import get_settings

from .deps import CurrentUser, current_adventure, router


@router.post("/{adventure_id}/summary/regenerate", response_model=schemas.AdventureOut)
async def regenerate_summary(
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
    adventure: models.Adventure = Depends(current_adventure),
):
    """Writes a new summary from the story, without reading the current one.

    This runs in the request rather than as a post-turn task. The player pressed
    a button and waits for the result. The call can take a while, because this
    reads an adventure with no memory bank in chunks. `memorybank.regenerate`
    limits the number of chunks.

    The endpoint refuses the shared demo key, for the reason the rest of the app
    refuses it. These are the player's own summarization calls, and the demo key
    pays for turns.
    """
    settings = get_settings(db, user)
    if auth.resolve_provider_config(settings).using_demo:
        raise HTTPException(400, "Regenerating the summary needs your own API key.")
    try:
        await memorybank.regenerate(adventure, settings, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(502, f"The summarizer failed: {exc}") from exc
    db.refresh(adventure)
    return adventure
