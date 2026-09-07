"""Rebuilding the story summary on demand.

There is no endpoint here for reading or editing the summary. It travels on the
adventure as `story_summary`, and the player's edits arrive through
`PATCH /adventures/{id}`, which `crud.update_adventure` routes into
`summaries.set_text`. This module is only the button that throws the current
version away and reads the story again.
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
    """Writes a new summary from the story, owing nothing to the current one.

    This runs in the request rather than as a post-turn task. The player pressed
    a button and is waiting to read the result, and an adventure with no memory
    bank is read in chunks, so the call can take a while. `memorybank.regenerate`
    caps how many chunks that is.

    The shared demo key is refused, for the reason the rest of the app refuses
    it: these are the player's own summarization calls, and the demo key pays
    for turns.
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
