from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import auth, limits, models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/story-cards", tags=["story-cards"])

# AI Dungeon world-info / story-card array format. Its field names differ from
# our columns: value<->entry, title<->name, description<->notes. The extra
# `useForCharacterCreation` flag has no equivalent here — ignored on import,
# emitted as false on export so round-tripping through AI Dungeon stays valid.


def _visible_owner(scenario_id, adventure_id, db, user):
    """Resolve the scenario/adventure a caller may *read* cards from (public
    demo scenarios included), or 404/422."""
    if (scenario_id is None) == (adventure_id is None):
        raise HTTPException(422, "Provide exactly one of scenario_id or adventure_id")
    if scenario_id is not None:
        scenario = db.get(models.Scenario, scenario_id)
        if scenario is None or (scenario.user_id != user.id and not scenario.is_public):
            raise HTTPException(404, "Owner not found")
        return scenario
    adventure = db.get(models.Adventure, adventure_id)
    if adventure is None or adventure.user_id != user.id:
        raise HTTPException(404, "Owner not found")
    return adventure


def _card_editable_or_404(card: models.StoryCard | None, user: models.User) -> models.StoryCard:
    """Cards inherit their scope from the owning scenario/adventure. Public
    (demo) scenarios are visible to everyone but editable by no one."""
    if card is not None:
        owner = card.scenario if card.scenario_id is not None else card.adventure
        if owner is not None and owner.user_id == user.id:
            return card
    raise HTTPException(404, "Story card not found")


@router.get("", response_model=list[schemas.StoryCardOut])
def list_story_cards(
    scenario_id: int | None = None,
    adventure_id: int | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    if (scenario_id is None) == (adventure_id is None):
        raise HTTPException(422, "Provide exactly one of scenario_id or adventure_id")
    if scenario_id is not None:
        scenario = db.get(models.Scenario, scenario_id)
        if scenario is None or (scenario.user_id != user.id and not scenario.is_public):
            raise HTTPException(404, "Owner not found")
        return sorted(scenario.story_cards, key=lambda c: c.id)
    adventure = db.get(models.Adventure, adventure_id)
    if adventure is None or adventure.user_id != user.id:
        raise HTTPException(404, "Owner not found")
    return sorted(adventure.story_cards, key=lambda c: c.id)


@router.post("", response_model=schemas.StoryCardOut, status_code=201)
def create_story_card(
    payload: schemas.StoryCardCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    if (payload.scenario_id is None) == (payload.adventure_id is None):
        raise HTTPException(422, "Provide exactly one of scenario_id or adventure_id")
    owner_model = models.Scenario if payload.scenario_id else models.Adventure
    owner_id = payload.scenario_id or payload.adventure_id
    owner = db.get(owner_model, owner_id)
    if owner is None or owner.user_id != user.id:
        raise HTTPException(404, "Owner not found")
    limits.check_row_cap(
        "story_cards", db, user,
        scenario_id=payload.scenario_id, adventure_id=payload.adventure_id,
    )
    card = models.StoryCard(**payload.model_dump())
    db.add(card)
    db.commit()
    return card


@router.patch("/{card_id}", response_model=schemas.StoryCardOut)
def update_story_card(
    card_id: int,
    payload: schemas.StoryCardUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    card = _card_editable_or_404(db.get(models.StoryCard, card_id), user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(card, field, value)
    db.commit()
    return card


@router.delete("/{card_id}", status_code=204)
def delete_story_card(
    card_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    card = _card_editable_or_404(db.get(models.StoryCard, card_id), user)
    db.delete(card)
    db.commit()


# ---------- Bulk import / export (AI Dungeon world-info format) ----------


@router.get("/export")
def export_story_cards(
    scenario_id: int | None = None,
    adventure_id: int | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    owner = _visible_owner(scenario_id, adventure_id, db, user)
    return [
        {
            "keys": c.keys,
            "value": c.entry,
            "type": c.type,
            "title": c.name,
            "description": c.notes,
            "useForCharacterCreation": False,
        }
        for c in sorted(owner.story_cards, key=lambda c: c.id)
    ]


@router.post("/import", response_model=list[schemas.StoryCardOut], status_code=201)
def import_story_cards(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Append a list of story cards (AI Dungeon world-info format, or our own
    export) to a scenario/adventure the caller owns."""
    scenario_id = payload.get("scenario_id")
    adventure_id = payload.get("adventure_id")
    if (scenario_id is None) == (adventure_id is None):
        raise HTTPException(422, "Provide exactly one of scenario_id or adventure_id")
    owner_model = models.Scenario if scenario_id is not None else models.Adventure
    owner = db.get(owner_model, scenario_id if scenario_id is not None else adventure_id)
    if owner is None or owner.user_id != user.id:
        raise HTTPException(404, "Owner not found")

    # Accept a bare array or {"cards": [...]} / {"storyCards": [...]}, so an
    # AI Dungeon world-info file dropped in as-is still works.
    cards_in = payload.get("cards") or payload.get("storyCards") or payload.get("worldInfo")
    if not isinstance(cards_in, list):
        raise HTTPException(422, 'Expected a "cards" array of story cards.')
    cards_in = [c for c in cards_in if isinstance(c, dict)]

    limits.rate_limit("import", request, user)
    limits.check_bundle_lists(story_cards=cards_in)
    existing = len(owner.story_cards)
    if auth.MULTI_USER and existing + len(cards_in) > limits.MAX_STORY_CARDS_PER_OWNER:
        raise HTTPException(
            409,
            f"Importing {len(cards_in)} would exceed the limit of "
            f"{limits.MAX_STORY_CARDS_PER_OWNER} story cards here "
            f"({existing} already present) — remove some first.",
        )

    created = []
    for card in cards_in:
        obj = models.StoryCard(
            scenario_id=scenario_id,
            adventure_id=adventure_id,
            type=str(card.get("type") or "")[:schemas.CARD_TYPE_MAX],
            name=str(card.get("title") or card.get("name") or "")[:schemas.NAME_MAX],
            keys=str(card.get("keys") or "")[:schemas.PROSE_MAX],
            entry=str(card.get("value") or card.get("entry") or "")[:schemas.PROSE_MAX],
            notes=str(card.get("description") or card.get("notes") or "")[:schemas.PROSE_MAX],
        )
        db.add(obj)
        created.append(obj)
    db.commit()
    return created
