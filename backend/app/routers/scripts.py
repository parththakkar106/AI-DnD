from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import auth, limits, models, schemas
from ..database import get_db
from ..scripting import run_hook

router = APIRouter(prefix="/api/scripts", tags=["scripts"])

HOOK_FIELDS = {"input": "input_js", "context": "context_js", "output": "output_js"}


def get_script_or_404(script_id: int, db: Session, user: models.User) -> models.Script:
    script = db.get(models.Script, script_id)
    if script is None or script.user_id != user.id:
        raise HTTPException(404, "Script not found")
    return script


@router.get("", response_model=list[schemas.ScriptOut])
def list_scripts(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    return (
        db.query(models.Script)
        .filter(models.Script.user_id == user.id)
        .order_by(models.Script.updated_at.desc())
        .all()
    )


@router.post("", response_model=schemas.ScriptOut, status_code=201)
def create_script(
    payload: schemas.ScriptCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    limits.check_row_cap("scripts", db, user)
    script = models.Script(**payload.model_dump(), user_id=user.id)
    db.add(script)
    db.commit()
    return script


@router.get("/{script_id}", response_model=schemas.ScriptOut)
def get_script(
    script_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    return get_script_or_404(script_id, db, user)


@router.patch("/{script_id}", response_model=schemas.ScriptOut)
def update_script(
    script_id: int,
    payload: schemas.ScriptUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    script = get_script_or_404(script_id, db, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(script, field, value)
    db.commit()
    return script


@router.delete("/{script_id}", status_code=204)
def delete_script(
    script_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    db.delete(get_script_or_404(script_id, db, user))
    db.commit()


@router.post("/{script_id}/test")
def test_script(
    script_id: int,
    payload: schemas.ScriptTestRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Dry-run one hook against sample text — no AI call, no persistence."""
    script = get_script_or_404(script_id, db, user)
    limits.rate_limit("script-test", request, user)
    result = run_hook(
        script.library_js,
        getattr(script, HOOK_FIELDS[payload.hook]),
        payload.text,
        payload.state,
        history=[],
        story_cards=[],
        info={"actionCount": 0, "characterNames": [], "memoryLength": 0, "maxChars": 0},
    )
    return {
        "text": result.text,
        "stop": result.stop,
        "state": result.state,
        "storyCards": result.story_cards,
        "logs": result.logs,
        "error": result.error,
    }


# ---------- Import / Export ----------

@router.get("/{script_id}/export")
def export_script(
    script_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """JSON bundle matching how AI Dungeon scripts circulate."""
    script = get_script_or_404(script_id, db, user)
    return {
        "name": script.name,
        "description": script.description,
        "library": script.library_js,
        "input": script.input_js,
        "context": script.context_js,
        "output": script.output_js,
    }


@router.post("/import", response_model=schemas.ScriptOut, status_code=201)
def import_script(
    request: Request,
    bundle: dict = Body(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Accepts our export bundle; tolerates *_js key names too."""
    limits.rate_limit("import", request, user)
    limits.check_row_cap("scripts", db, user)
    def pick(*keys: str) -> str:
        for key in keys:
            value = bundle.get(key)
            if isinstance(value, str):
                return value
        return ""

    script = models.Script(
        user_id=user.id,
        # Raw-dict import bypasses the schemas — clamp to the VARCHAR width.
        name=(pick("name") or "Imported Script")[:schemas.NAME_MAX],
        description=pick("description"),
        library_js=pick("library", "library_js", "sharedLibrary"),
        input_js=pick("input", "input_js", "onInput"),
        context_js=pick("context", "context_js", "onModelContext"),
        output_js=pick("output", "output_js", "onOutput"),
    )
    db.add(script)
    db.commit()
    return script
