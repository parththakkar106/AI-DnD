from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/story-cards", tags=["story-cards"])


@router.get("", response_model=list[schemas.StoryCardOut])
def list_story_cards(
    scenario_id: int | None = None,
    adventure_id: int | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(models.StoryCard)
    if scenario_id is not None:
        query = query.filter(models.StoryCard.scenario_id == scenario_id)
    if adventure_id is not None:
        query = query.filter(models.StoryCard.adventure_id == adventure_id)
    return query.order_by(models.StoryCard.id).all()


@router.post("", response_model=schemas.StoryCardOut, status_code=201)
def create_story_card(payload: schemas.StoryCardCreate, db: Session = Depends(get_db)):
    if (payload.scenario_id is None) == (payload.adventure_id is None):
        raise HTTPException(422, "Provide exactly one of scenario_id or adventure_id")
    owner_model = models.Scenario if payload.scenario_id else models.Adventure
    owner_id = payload.scenario_id or payload.adventure_id
    if db.get(owner_model, owner_id) is None:
        raise HTTPException(404, "Owner not found")
    card = models.StoryCard(**payload.model_dump())
    db.add(card)
    db.commit()
    return card


@router.patch("/{card_id}", response_model=schemas.StoryCardOut)
def update_story_card(
    card_id: int, payload: schemas.StoryCardUpdate, db: Session = Depends(get_db)
):
    card = db.get(models.StoryCard, card_id)
    if card is None:
        raise HTTPException(404, "Story card not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(card, field, value)
    db.commit()
    return card


@router.delete("/{card_id}", status_code=204)
def delete_story_card(card_id: int, db: Session = Depends(get_db)):
    card = db.get(models.StoryCard, card_id)
    if card is None:
        raise HTTPException(404, "Story card not found")
    db.delete(card)
    db.commit()
