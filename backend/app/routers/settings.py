import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import auth, models, schemas, security
from ..database import get_db

router = APIRouter(prefix="/api/settings", tags=["settings"])


def get_settings(db: Session, user: models.User) -> models.Settings:
    """Per-user settings row, created on first access (Phase 8: settings —
    endpoint, key, models, memory config — are per user, not global)."""
    settings = (
        db.query(models.Settings).filter(models.Settings.user_id == user.id).first()
    )
    if settings is None:
        settings = models.Settings(user_id=user.id)
        db.add(settings)
        db.commit()
    return settings


@router.get("", response_model=schemas.SettingsOut)
def read_settings(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    return get_settings(db, user)


@router.put("", response_model=schemas.SettingsOut)
def update_settings(
    payload: schemas.SettingsUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    settings = get_settings(db, user)
    fields = payload.model_dump(exclude_unset=True)
    # Write-only API key: absent = unchanged, "" = cleared, else encrypted.
    if "api_key" in fields:
        fields["api_key"] = security.encrypt_secret(fields["api_key"].strip())
    embedding_model_changed = (
        "embedding_model" in fields
        and fields["embedding_model"] != settings.embedding_model
    )
    for field, value in fields.items():
        setattr(settings, field, value)
    if embedding_model_changed:
        # Vectors from the old model have a different dimensionality/space;
        # clear them so the post-turn task re-embeds with the new model.
        # (This user's adventures only — settings are per-user now.)
        owned = (
            db.query(models.Adventure.id)
            .filter(models.Adventure.user_id == user.id)
            .scalar_subquery()
        )
        db.query(models.Memory).filter(models.Memory.adventure_id.in_(owned)).update(
            {"embedding": None}, synchronize_session=False
        )
    db.commit()
    return settings


@router.post("/test")
async def test_connection(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Hit the endpoint's /models listing as a cheap connectivity check.
    Tests whatever the turn engine would actually use — including the shared
    demo endpoint when the user has no key of their own."""
    settings = get_settings(db, user)
    cfg = auth.resolve_provider_config(settings)
    url = cfg.endpoint_url.rstrip("/") + "/models"
    headers = {}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
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
