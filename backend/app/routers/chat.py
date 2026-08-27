"""AI Chat: a plain scratchpad for talking to a model directly.

Power users reach it, which means the `AIDND_POWER_USERS` email allowlist. It is
deliberately thin. It adds no story context, no scripts, and no world state, and
it persists nothing. The conversation lives in the browser and is posted in full
on each turn. It exists for testing models, prompts, and endpoints without
starting an adventure.

Model choice is free when the user brought their own API key. On the shared demo
key the model stays pinned to the `AIDND_DEMO_MODELS` allowlist, exactly as it is
for turns. The server funds that key, so this page must not let it reach paid
models.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import auth, limits, models, schemas
from ..database import get_db
from ..providers import OpenAICompatibleProvider, ProviderError
from .adventures import SSE_HEADERS, sse
from .settings import get_settings, list_endpoint_models

router = APIRouter(prefix="/api/chat", tags=["chat"])


def power_user(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.get_current_user),
) -> models.User:
    """Gate for the whole router. 404 rather than 403 so the feature simply
    doesn't appear to exist for everyone else."""
    if not auth.is_power_user(user):
        raise HTTPException(404, "Not found")
    return user


PowerUser = Depends(power_user)


def _resolve_model(
    settings: models.Settings, requested: str | None
) -> tuple[auth.ProviderConfig, str | None]:
    """Returns the provider config for this chat, plus a note when the requested
    model was not used.

    The pinning rule lives in `resolve_provider_config`. This function only
    reports the substitution that call made, so one place decides what the demo
    key may talk to.
    """
    cfg = auth.resolve_provider_config(settings, model_override=requested)
    wanted = (requested or "").strip()
    if wanted and wanted != cfg.model:
        return cfg, (
            f"'{wanted}' isn't available on the shared demo key — using "
            f"{cfg.model}. Add your own API key in Settings to use any model."
        )
    return cfg, None


@router.get("/config")
async def chat_config(
    db: Session = Depends(get_db),
    user: models.User = PowerUser,
):
    """Returns what this page can talk to.

    The response holds the resolved endpoint and model, whether model choice is
    pinned to the demo allowlist, and the endpoint's model listing. The listing
    is best effort, and an unreachable endpoint returns an empty list.
    """
    settings = get_settings(db, user)
    cfg = auth.resolve_provider_config(settings)
    listing = await list_endpoint_models(cfg)
    return {
        "endpoint_url": cfg.endpoint_url,
        "model": cfg.model,
        "using_demo": cfg.using_demo,
        "api_mode": settings.api_mode,
        "temperature": settings.temperature,
        "max_tokens": settings.max_output_tokens,
        # On the demo key the whitelist IS the list of choices; otherwise it's
        # whatever the endpoint advertises (suggestions, not a restriction).
        "models": auth.DEMO_MODELS if cfg.using_demo else listing.get("models", []),
        "models_error": None if listing.get("ok") else listing.get("detail"),
    }


async def run_chat(cfg: auth.ProviderConfig, settings: models.Settings, payload: schemas.ChatRequest,
                   note: str | None, db: Session, user: models.User):
    """Streams the reply as SSE, using the turn stream's event shape.

    The generator emits `reasoning` and `chunk` events while generating and then
    a `done` event, so the frontend reuses the same code.
    """
    if note:
        yield sse({"type": "note", "detail": note})
    provider = OpenAICompatibleProvider(
        cfg.endpoint_url, cfg.api_key, cfg.model, settings.api_mode,
        settings.reasoning_max_tokens,
    )
    messages = [m.model_dump() for m in payload.messages]
    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    try:
        async for kind, chunk in provider.chat(
            messages,
            temperature=payload.temperature if payload.temperature is not None else settings.temperature,
            max_tokens=payload.max_tokens or settings.max_output_tokens,
        ):
            if kind == "reasoning":
                reasoning_chunks.append(chunk)
                yield sse({"type": "reasoning", "text": chunk})
            else:
                chunks.append(chunk)
                yield sse({"type": "chunk", "text": chunk})
    except ProviderError as exc:
        yield sse({"type": "error", "detail": str(exc)})
        return

    text = "".join(chunks).strip()
    if not text:
        detail = (
            "The model used its entire token budget on reasoning and returned no "
            "reply — raise max tokens, cap the reasoning budget in Settings, or "
            "use a non-reasoning model."
            if reasoning_chunks
            else "The AI returned an empty response."
        )
        yield sse({"type": "error", "detail": detail})
        return

    if cfg.using_demo:
        # Unmetered for power users (count_demo_turn is a no-op for them), but
        # keep the call so the accounting stays right if the gate ever widens.
        auth.count_demo_turn(user)
        db.commit()
    yield sse({
        "type": "done",
        "text": text,
        "reasoning": "".join(reasoning_chunks).strip() or None,
        "model": cfg.model,
    })


@router.post("/stream")
def chat_stream(
    payload: schemas.ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = PowerUser,
):
    total = sum(len(m.content) for m in payload.messages)
    if total > schemas.CHAT_TOTAL_MAX:
        raise HTTPException(
            413, f"This conversation is too long to send ({total:,} characters) — "
                 "clear it or start a new one."
        )
    limits.rate_limit("chat", request, user)
    settings = get_settings(db, user)
    cfg, note = _resolve_model(settings, payload.model)
    if not cfg.model:
        raise HTTPException(400, "No model configured — set one in Settings or pick one here.")
    return StreamingResponse(
        run_chat(cfg, settings, payload, note, db, user),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
