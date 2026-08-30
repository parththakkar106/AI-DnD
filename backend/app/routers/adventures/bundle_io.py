"""Exporting an adventure to a bundle, and importing one back.

`app/bundle.py` owns the format and the version handling. These two endpoints
only check ownership and hand the work over.
"""

from fastapi import Body, Depends, Request
from sqlalchemy.orm import Session

from ... import analytics, bundle, limits, models, schemas
from ...database import get_db

from .deps import CurrentUser, current_adventure, router


@router.get("/{adventure_id}/export")
def export_adventure(
    db: Session = Depends(get_db),
    adv: models.Adventure = Depends(current_adventure),
):
    """Returns a full backup: plot components, story cards, scripts, state, and tree.

    `app/bundle.py` owns the format, in both of its versions. A backup outlives
    the schema, so no call site decides anything about its shape.
    """
    return bundle.export(db, adv)


@router.post("/import", response_model=schemas.AdventureOut, status_code=201)
def import_adventure(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    version = bundle.check_format(payload)
    limits.rate_limit("import", request, user)
    limits.check_row_cap("adventures", db, user)
    limits.check_bundle_lists(
        story_cards=payload.get("storyCards"),
        memories=payload.get("memories"),
        actions=payload.get("actions"),
        branches=payload.get("branches"),
    )
    # Check the tree before the adventure row exists, so that an inconsistent
    # file returns a 400 rather than leaving a half-imported adventure with a
    # gap in its story.
    story = bundle.plan(payload, version)
    # Count again, this time over what is written. The check above reads the
    # file's own lists, and in a v1 file one turn is one entry that carries its
    # retries in a `variants` array. `plan()` expands that into one row per
    # attempt, because SP4 made every attempt a node. A file of 5,000 turns with
    # ten attempts each therefore passes a 5,000-action cap and writes 50,000
    # rows, well inside the 20 MB body limit. `plan()` has no side effects and
    # the adventure does not exist yet, so this check costs only the planning.
    limits.check_bundle_lists(
        actions=story["nodes"],
        memories=story["memories"],
        branches=story["branches"],
    )

    adventure = bundle.materialize(db, payload, story, user.id)

    db.commit()
    db.refresh(adventure)
    # This is not a funnel step. A returning player imports a bundle, so it
    # says nothing about how far a first-time visitor got. It is counted anyway,
    # because it is the clearest evidence that anyone uses the export format.
    analytics.record_event(analytics.EV_IMPORT, user)
    return adventure
