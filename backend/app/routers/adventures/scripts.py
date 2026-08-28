"""The per-adventure copies of library scripts.

An adventure snapshots a library `Script` when it starts, so editing the library
does not change a story in progress. These endpoints report whether a snapshot
has fallen behind its library original, and copy the original over on request.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ... import models, schemas
from ...database import get_db

from .deps import CurrentUser, current_adventure, router


# Fields that are copied from a library Script into its adventure-script
# snapshot, and compared to decide whether a copy is out of date.
SYNC_FIELDS = ("name", "description", "library_js", "input_js", "context_js", "output_js")


def resolve_library_script(
    adv_script: models.AdventureScript, db: Session, user: models.User
) -> models.Script | None:
    """Returns the library Script an adventure script can re-sync from.

    The result is the script this copy was made from. For a legacy copy with no
    link, it is one of the player's own scripts with the same name. Only the
    player's own scripts are considered, so a copy derived from a demo scenario
    has nothing to sync to.
    """
    if adv_script.source_script_id is not None:
        script = db.get(models.Script, adv_script.source_script_id)
        if script is not None and script.user_id == user.id:
            return script
    return (
        db.query(models.Script)
        .filter(models.Script.user_id == user.id, models.Script.name == adv_script.name)
        .order_by(models.Script.updated_at.desc())
        .first()
    )


def _mark_out_of_date(
    adv_script: models.AdventureScript, db: Session, user: models.User
) -> models.AdventureScript:
    """Attaches a transient `out_of_date` flag, which `AdventureScriptOut` reads.

    The flag is `True` or `False` when a syncable library version exists, and
    `None` when none exists.
    """
    library = resolve_library_script(adv_script, db, user)
    adv_script.out_of_date = (
        None if library is None
        else any(getattr(adv_script, f) != getattr(library, f) for f in SYNC_FIELDS)
    )
    return adv_script


@router.get("/{adventure_id}/scripts", response_model=list[schemas.AdventureScriptOut])
def list_adventure_scripts(
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
    adventure: models.Adventure = Depends(current_adventure),
):
    return [_mark_out_of_date(s, db, user) for s in adventure.scripts]


@router.post(
    "/{adventure_id}/scripts/{adv_script_id}/sync",
    response_model=schemas.AdventureScriptOut,
)
def sync_adventure_script(
    adventure_id: int,
    adv_script_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
    adventure: models.Adventure = Depends(current_adventure),
):
    """Overwrites this copy's code with the latest from its library script.

    `enabled`, `position`, and the adventure's shared `script_state` are kept.
    """
    script = db.get(models.AdventureScript, adv_script_id)
    if script is None or script.adventure_id != adventure_id:
        raise HTTPException(404, "Script not found")
    library = resolve_library_script(script, db, user)
    if library is None:
        raise HTTPException(404, "No library script to sync from")
    for field in SYNC_FIELDS:
        setattr(script, field, getattr(library, field))
    # Store the link, so that a name-matched legacy copy syncs by id next
    # time.
    script.source_script_id = library.id
    db.commit()
    db.refresh(script)
    return _mark_out_of_date(script, db, user)


@router.patch(
    "/{adventure_id}/scripts/{adv_script_id}", response_model=schemas.AdventureScriptOut
)
def update_adventure_script(
    adventure_id: int,
    adv_script_id: int,
    payload: schemas.AdventureScriptUpdate,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    script = db.get(models.AdventureScript, adv_script_id)
    if script is None or script.adventure_id != adventure_id:
        raise HTTPException(404, "Script not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(script, field, value)
    db.commit()
    return script
