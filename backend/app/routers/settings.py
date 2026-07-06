import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/settings", tags=["settings"])


def get_settings(db: Session) -> models.Settings:
    settings = db.get(models.Settings, 1)
    if settings is None:
        settings = models.Settings(id=1)
        db.add(settings)
        db.commit()
    return settings


@router.get("", response_model=schemas.SettingsOut)
def read_settings(db: Session = Depends(get_db)):
    return get_settings(db)


@router.put("", response_model=schemas.SettingsOut)
def update_settings(payload: schemas.SettingsUpdate, db: Session = Depends(get_db)):
    settings = get_settings(db)
    fields = payload.model_dump(exclude_unset=True)
    embedding_model_changed = (
        "embedding_model" in fields
        and fields["embedding_model"] != settings.embedding_model
    )
    for field, value in fields.items():
        setattr(settings, field, value)
    if embedding_model_changed:
        # Vectors from the old model have a different dimensionality/space;
        # clear them so the post-turn task re-embeds with the new model.
        db.query(models.Memory).update({"embedding": None})
    db.commit()
    return settings


@router.post("/test")
async def test_connection(db: Session = Depends(get_db)):
    """Hit the endpoint's /models listing as a cheap connectivity check."""
    settings = get_settings(db)
    url = settings.endpoint_url.rstrip("/") + "/models"
    headers = {}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Connection failed: {exc}"}

    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}

    models_available: list[str] = []
    try:
        data = resp.json()
        models_available = [m.get("id", "?") for m in data.get("data", [])]
    except (ValueError, AttributeError, TypeError):
        pass  # non-JSON or unexpected shape — connectivity is still confirmed
    return {"ok": True, "models": models_available}
