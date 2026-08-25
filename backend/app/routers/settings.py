import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import auth, limits, models, netguard, schemas, security
from ..database import get_db

router = APIRouter(prefix="/api/settings", tags=["settings"])


def get_settings(db: Session, user: models.User) -> models.Settings:
    """Returns the user's settings row, creating it on first access.

    Phase 8 made settings per user rather than global. They cover the endpoint,
    the key, the models, and the memory configuration.
    """
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
        # This covers only this user's adventures, because settings are per
        # user now.
        #
        # Both columns, and the flag. This is the one place that clears vectors
        # in bulk rather than through memorybank.set_vector, and when the
        # vectors moved to embedding_blob it kept nulling the old JSON column
        # alone. The blob survived, `embedded` stayed true, and
        # `_embed_pending`, which selects rows where `embedded IS FALSE`, never
        # found the rows. The bank kept ranking against the previous model's
        # vectors.
        owned = (
            db.query(models.Adventure.id)
            .filter(models.Adventure.user_id == user.id)
            .scalar_subquery()
        )
        db.query(models.Memory).filter(models.Memory.adventure_id.in_(owned)).update(
            {"embedding_blob": None, "embedded": False}, synchronize_session=False
        )
        # No cache invalidation needed, and deliberately none added: clearing
        # `embedded` drops these rows out of the catalogue query, so retrieval
        # stops asking for them, and by the time _embed_pending puts one back
        # it has gone through set_vector, which evicts that entry. The rule
        # holds: anything that removes a memory from play corrects itself.
    db.commit()
    return settings


async def list_endpoint_models(cfg: auth.ProviderConfig) -> dict:
    """Fetches the endpoint's /models listing.

    The call also serves as a connectivity check, so a failure returns
    `{"ok": False, "detail": ...}` rather than raising.
    """
    # SSRF guard. Never probe a non-public address the user supplied.
    reason = await run_in_threadpool(netguard.endpoint_block_reason, cfg.endpoint_url)
    if reason:
        return {"ok": False, "detail": f"Can't reach that endpoint — {reason}."}
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
        pass  # The body is not JSON or has an unexpected shape. The endpoint
        # is still reachable.
    return {"ok": True, "models": models_available}


@router.post("/test")
async def test_connection(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
):
    """Runs a cheap connectivity check against whatever the turn engine would use.

    That includes the shared demo endpoint, when the user has no key of their
    own.
    """
    limits.rate_limit("connection-test", request, user)
    settings = get_settings(db, user)
    return await list_endpoint_models(auth.resolve_provider_config(settings))
