from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import auth, limits, models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/story-cards", tags=["story-cards"])


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
