from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


def get_scenario_or_404(scenario_id: int, db: Session) -> models.Scenario:
    scenario = db.get(models.Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(404, "Scenario not found")
    return scenario


@router.get("", response_model=list[schemas.ScenarioListItem])
def list_scenarios(db: Session = Depends(get_db)):
    return (
        db.query(models.Scenario)
        .order_by(models.Scenario.updated_at.desc())
        .all()
    )


@router.post("", response_model=schemas.ScenarioOut, status_code=201)
def create_scenario(payload: schemas.ScenarioCreate, db: Session = Depends(get_db)):
    scenario = models.Scenario(**payload.model_dump())
    db.add(scenario)
    db.commit()
    return scenario


@router.get("/{scenario_id}", response_model=schemas.ScenarioOut)
def get_scenario(scenario_id: int, db: Session = Depends(get_db)):
    return get_scenario_or_404(scenario_id, db)


@router.patch("/{scenario_id}", response_model=schemas.ScenarioOut)
def update_scenario(
    scenario_id: int, payload: schemas.ScenarioUpdate, db: Session = Depends(get_db)
):
    scenario = get_scenario_or_404(scenario_id, db)
    data = payload.model_dump(exclude_unset=True)
    script_ids = data.pop("script_ids", None)
    for field, value in data.items():
        setattr(scenario, field, value)
    if script_ids is not None:
        scripts = db.query(models.Script).filter(models.Script.id.in_(script_ids)).all()
        if len(scripts) != len(set(script_ids)):
            raise HTTPException(404, "One or more scripts not found")
        scenario.scripts = sorted(scripts, key=lambda s: script_ids.index(s.id))
    db.commit()
    return scenario


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: int, db: Session = Depends(get_db)):
    scenario = get_scenario_or_404(scenario_id, db)
    db.delete(scenario)
    db.commit()


# ---------- Import / Export ----------

@router.get("/{scenario_id}/export")
def export_scenario(scenario_id: int, db: Session = Depends(get_db)):
    s = get_scenario_or_404(scenario_id, db)
    return {
        "format": "ai-dnd-scenario-v1",
        "title": s.title,
        "description": s.description,
        "prompt": s.prompt,
        "memory": s.memory,
        "authorsNote": s.authors_note,
        "aiInstructions": s.ai_instructions,
        "tags": s.tags,
        "storyCards": [
            {"type": c.type, "name": c.name, "keys": c.keys, "entry": c.entry, "notes": c.notes}
            for c in s.story_cards
        ],
        "scripts": [
            {
                "name": sc.name, "description": sc.description, "library": sc.library_js,
                "input": sc.input_js, "context": sc.context_js, "output": sc.output_js,
            }
            for sc in s.scripts
        ],
    }


# Key aliases seen in AI Dungeon scenario exports, mapped best-effort.
_SCENARIO_KEYS = {
    "title": "title",
    "description": "description",
    "prompt": "prompt",
    "memory": "memory",
    "authorsNote": "authors_note",
    "authors_note": "authors_note",
    "authorsNoteText": "authors_note",
    "aiInstructions": "ai_instructions",
    "ai_instructions": "ai_instructions",
    "instructions": "ai_instructions",
}
_IGNORED_KEYS = {"format", "storyCards", "worldInfo", "worldInformation", "scripts", "tags",
                 "createdAt", "updatedAt", "id", "publicId", "image", "nsfw", "type", "options"}


@router.post("/import", status_code=201)
def import_scenario(bundle: dict = Body(...), db: Session = Depends(get_db)):
    """Accepts our export format and AI Dungeon scenario exports best-effort;
    reports any keys it didn't understand."""
    fields: dict = {}
    unmapped: list[str] = []
    for key, value in bundle.items():
        if key in _SCENARIO_KEYS and isinstance(value, str):
            fields[_SCENARIO_KEYS[key]] = value
        elif key not in _IGNORED_KEYS:
            unmapped.append(key)

    tags = bundle.get("tags")
    if isinstance(tags, list):
        fields["tags"] = ", ".join(str(t) for t in tags)
    elif isinstance(tags, str):
        fields["tags"] = tags

    scenario = models.Scenario(**fields)
    if not scenario.title:
        scenario.title = "Imported Scenario"
    db.add(scenario)
    db.flush()

    # AI Dungeon exports have used all three names for the same list.
    cards = (
        bundle.get("storyCards")
        or bundle.get("worldInfo")
        or bundle.get("worldInformation")
        or []
    )
    for card in cards:
        if not isinstance(card, dict):
            continue
        db.add(
            models.StoryCard(
                scenario_id=scenario.id,
                type=str(card.get("type") or ""),
                name=str(card.get("name") or card.get("title") or ""),
                keys=str(card.get("keys") or ""),
                # AI Dungeon world info uses "value"; story cards use "entry".
                entry=str(card.get("entry") or card.get("value") or ""),
                notes=str(card.get("notes") or card.get("description") or ""),
            )
        )

    for item in bundle.get("scripts") or []:
        if not isinstance(item, dict):
            continue
        script = models.Script(
            name=str(item.get("name") or "Imported Script"),
            description=str(item.get("description") or ""),
            library_js=str(item.get("library") or item.get("sharedLibrary") or ""),
            input_js=str(item.get("input") or item.get("onInput") or ""),
            context_js=str(item.get("context") or item.get("onModelContext") or ""),
            output_js=str(item.get("output") or item.get("onOutput") or ""),
        )
        db.add(script)
        db.flush()
        scenario.scripts.append(script)

    db.commit()
    out = schemas.ScenarioOut.model_validate(scenario).model_dump(mode="json")
    return {"scenario": out, "unmapped_keys": unmapped}
