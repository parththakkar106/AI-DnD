import copy
import json
import re
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, undefer

from .. import auth, images, limits, memorybank, models, schemas, worldstate
from ..context import build_context
from ..context import history as context_history
from ..database import get_db
from ..providers import OpenAICompatibleProvider, PromptParts, ProviderError
from ..scripting import ScriptPipeline
from .settings import get_settings

router = APIRouter(prefix="/api/adventures", tags=["adventures"])

CurrentUser = Depends(auth.get_current_user)


def get_adventure_or_404(
    adventure_id: int, db: Session, user: models.User
) -> models.Adventure:
    adventure = db.get(models.Adventure, adventure_id)
    if adventure is None or adventure.user_id != user.id:
        raise HTTPException(404, "Adventure not found")
    return adventure


# How much of the last narrative beat a Continue card shows. Long enough to
# re-establish the scene, short enough that the card stays a card.
SNIPPET_MAX = 220


def _snippet(text: str) -> str:
    """Condense stored action text into one flowing line for a card."""
    # Stored AI text already has any world-state block stripped (see the
    # streaming handler below), so this only has to tidy whitespace.
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= SNIPPET_MAX:
        return collapsed
    # Cut on a word boundary rather than mid-word, then let CSS add the ellipsis.
    cut = collapsed[:SNIPPET_MAX].rsplit(" ", 1)[0]
    return f"{cut}…"


# Action types that read as narration. `start` is the scenario's opening prompt,
# which is the only text a freshly-created adventure has — without it a brand-new
# story's card would claim nothing had been written yet. `do`/`say` are excluded:
# "where you left off" should be the story's voice, not the player's.
NARRATION_TYPES = ("ai", "story", "start")


def _latest_narration(db: Session, adventure_ids: list[int]) -> dict[int, str]:
    """Map adventure id -> text of its most recent narrated action.

    One window-function query rather than a per-adventure lookup, so the list
    endpoint stays at a fixed number of round trips.
    """
    if not adventure_ids:
        return {}
    ranked = (
        db.query(
            models.Action.adventure_id.label("adventure_id"),
            models.Action.text.label("text"),
            func.row_number()
            .over(
                partition_by=models.Action.adventure_id,
                order_by=(models.Action.index.desc(), models.Action.id.desc()),
            )
            .label("rank"),
        )
        .filter(
            models.Action.adventure_id.in_(adventure_ids),
            models.Action.type.in_(NARRATION_TYPES),
        )
        .subquery()
    )
    rows = db.query(ranked.c.adventure_id, ranked.c.text).filter(ranked.c.rank == 1).all()
    return {adventure_id: text for adventure_id, text in rows}


@router.get("", response_model=list[schemas.AdventureListItem])
def list_adventures(db: Session = Depends(get_db), user: models.User = CurrentUser):
    rows = (
        db.query(
            models.Adventure,
            func.count(models.Action.id),
            models.Scenario.title,
            models.Scenario.image,
            models.Scenario.icon,
            models.Scenario.updated_at,
        )
        .outerjoin(models.Action)
        .outerjoin(models.Scenario, models.Adventure.scenario_id == models.Scenario.id)
        .filter(models.Adventure.user_id == user.id)
        # Group by both PKs: Postgres requires every selected column to be
        # grouped or aggregated. Adventure.* rides on its own grouped PK, but
        # the Scenario columns come from a joined table and must be listed too
        # (SQLite is lax here; Postgres rejects it).
        .group_by(
            models.Adventure.id,
            models.Scenario.id,
            models.Scenario.title,
            models.Scenario.image,
            models.Scenario.icon,
            models.Scenario.updated_at,
        )
        .order_by(models.Adventure.updated_at.desc())
        .all()
    )
    narration = _latest_narration(db, [adv.id for adv, *_ in rows])
    return [
        schemas.AdventureListItem(
            id=adv.id,
            scenario_id=adv.scenario_id,
            scenario_title=scenario_title,
            title=adv.title,
            updated_at=adv.updated_at,
            action_count=count,
            snippet=_snippet(narration.get(adv.id, "")),
            # The art belongs to the scenario, so the cache-busting stamp is the
            # scenario's updated_at, not the adventure's.
            image_url=images.public_url(adv.scenario_id, image or "", scenario_updated),
            icon=icon or "",
        )
        for adv, count, scenario_title, image, icon, scenario_updated in rows
    ]


PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Replace ${Name} with the player-provided value; unknown names are left as-is."""
    if not text or not values:
        return text
    return PLACEHOLDER_RE.sub(
        lambda m: values.get(m.group(1).strip(), m.group(0)), text
    )


# Adventure fields that start as a copy of the scenario's text and so can be
# re-copied by "Update from scenario". `title` is excluded on purpose: it is the
# adventure's own name, which players rename, and `story_summary` is play output,
# not scenario content.
SCENARIO_TEXT_FIELDS = ("memory", "authors_note", "ai_instructions")

# Story-card fields copied from the scenario, and compared to detect drift.
CARD_FIELDS = ("type", "name", "keys", "entry", "notes")


def scenario_card_specs(scenario: models.Scenario, values: dict[str, str]) -> dict[str, dict]:
    """Every story card a scenario implies, keyed by a stable `source_ref`:
    its own cards ("card:<id>") plus one per NPC defined in its stat_schema
    ("npc:<key>"), with placeholders already filled in.

    Shared by adventure creation and refresh so the two can't drift.
    """
    specs: dict[str, dict] = {}
    existing_names = {(c.name or "").strip().lower() for c in scenario.story_cards}
    for card in scenario.story_cards:
        specs[f"card:{card.id}"] = {
            "type": card.type,
            "name": card.name,
            "keys": fill_placeholders(card.keys, values),
            "entry": fill_placeholders(card.entry, values),
            "notes": card.notes,
        }
    # Phase 12: each defined NPC gets a story card (for its description as lore +
    # in-scene triggering), unless a card with that name already exists.
    for npc_key, ndef in (scenario.stat_schema or {}).get("npcs", {}).items():
        if not isinstance(ndef, dict):
            continue
        name = worldstate.npc_name(ndef, npc_key)
        if name.strip().lower() in existing_names:
            continue
        specs[f"npc:{npc_key}"] = {
            "type": "character",
            "name": name,
            "keys": fill_placeholders(str(ndef.get("keys") or name), values),
            "entry": fill_placeholders(str(ndef.get("desc") or ""), values),
            "notes": "",
        }
    return specs


@router.post("", response_model=schemas.AdventureOut, status_code=201)
def create_adventure(
    payload: schemas.AdventureCreate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    limits.check_row_cap("adventures", db, user)
    scenario = None
    if payload.scenario_id is not None:
        scenario = db.get(models.Scenario, payload.scenario_id)
        # Playable = your own scenario or a shared demo one.
        if scenario is None or (scenario.user_id != user.id and not scenario.is_public):
            raise HTTPException(404, "Scenario not found")

    values = payload.placeholders
    adventure = models.Adventure(
        user_id=user.id,
        scenario_id=scenario.id if scenario else None,
        title=payload.title or (scenario.title if scenario else "Untitled Adventure"),
        memory=fill_placeholders(scenario.memory, values) if scenario else "",
        authors_note=fill_placeholders(scenario.authors_note, values) if scenario else "",
        ai_instructions=fill_placeholders(scenario.ai_instructions, values) if scenario else "",
        # Phase 12: seed the live RPG state from the scenario's template.
        world_state=worldstate.instantiate(scenario.stat_schema) if scenario else {},
        # Kept so a later "Update from scenario" can re-fill re-copied text with
        # the same answers instead of re-injecting literal ${...} tokens.
        placeholders=dict(values),
    )
    db.add(adventure)
    db.flush()

    if scenario:
        for ref, spec in scenario_card_specs(scenario, values).items():
            db.add(models.StoryCard(adventure_id=adventure.id, source_ref=ref, **spec))
        for position, script in enumerate(scenario.scripts):
            db.add(
                models.AdventureScript(
                    adventure_id=adventure.id,
                    source_script_id=script.id,
                    position=position,
                    name=script.name,
                    description=script.description,
                    library_js=script.library_js,
                    input_js=script.input_js,
                    context_js=script.context_js,
                    output_js=script.output_js,
                )
            )
        if scenario.prompt.strip():
            db.add(
                models.Action(
                    adventure_id=adventure.id,
                    index=0,
                    type="start",
                    text=fill_placeholders(scenario.prompt, values),
                )
            )

    db.commit()
    db.refresh(adventure)
    return adventure


@router.get("/{adventure_id}", response_model=schemas.AdventureOut)
def get_adventure(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    return get_adventure_or_404(adventure_id, db, user)


@router.get("/{adventure_id}/script-state")
def get_script_state(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """The scripting `state` object — every variable scripts read/write via
    `state.x`, persisted after each hook. Empty {} until a script sets one."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    return {"state": state}


@router.get("/{adventure_id}/world-state")
def get_world_state(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """The RPG world state (live values) plus the scenario's stat_schema, so the
    play view can render the sheet + milestones. `schema` is null with no RPG layer."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    schema = adventure.scenario.stat_schema if adventure.scenario else None
    state = adventure.world_state if isinstance(adventure.world_state, dict) else {}
    return {
        "state": state,
        "schema": schema if worldstate.has_schema(schema) else None,
    }


@router.put("/{adventure_id}/world-state")
def override_world_state(
    adventure_id: int,
    overrides: dict = Body(...),
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Directly edit the live RPG values (a manual correction, not a turn).
    `overrides` maps paths (e.g. "player.hp", "npc.gwen.trust", "flags.x",
    "milestones.y") to their new absolute value. Unknown paths/wrong types are
    rejected individually; the rest still apply."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    schema = adventure.scenario.stat_schema if adventure.scenario else None
    if not worldstate.has_schema(schema):
        raise HTTPException(400, "This adventure has no RPG world-state layer")
    state = adventure.world_state if isinstance(adventure.world_state, dict) else {}
    new_state, report = worldstate.apply_override(state, schema, overrides)
    adventure.world_state = new_state
    db.commit()
    return {"state": new_state, "report": report}


def snapshot_state(adventure: models.Adventure) -> dict:
    """Deep copy of the shared script_state, to staple onto an action so undo/
    retry can restore it. Independent of later hook mutations."""
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    return copy.deepcopy(state)


def snapshot_world_state(adventure: models.Adventure) -> dict:
    """Deep copy of the RPG world_state, for the same undo/retry rollback as
    snapshot_state (Phase 12)."""
    state = adventure.world_state if isinstance(adventure.world_state, dict) else {}
    return copy.deepcopy(state)


# ---------- Retry history (variants) ----------
#
# Retry used to delete the AI action and generate a replacement. Now the row
# survives and every attempt is appended to `Action.variants`, with
# `variant_index` naming the live one. A variant carries only the parts that
# actually differ between attempts — the narration and the state it produced —
# never the assembled prompt, which is identical across attempts of one turn
# and is by far the biggest thing in `context_snapshot`.

# The per-attempt slices of context_snapshot. Everything else in the snapshot
# (system/story/memories) is shared by every attempt at the same turn.
VARIANT_SNAPSHOT_KEYS = ("world_state", "script", "raw_output")


def world_delta_of(snapshot: dict | None) -> dict | None:
    """The bulk-read slice of a context snapshot, for Action.world_delta.

    context_snapshot is deferred (it holds the whole assembled prompt), so the
    two things that ARE needed for every action — the world-change chips and
    the emit block replayed into history — get their own small column. Keep
    this in step with the snapshot wherever one is written."""
    ws = (snapshot or {}).get("world_state")
    if not isinstance(ws, dict):
        return None
    return {
        "delta": ws.get("delta") or {},
        "applied": (ws.get("report") or {}).get("applied") or [],
    }


def set_variants(action: models.Action, entries: list[dict]) -> None:
    """The ONLY way to write Action.variants.

    `variants` is deferred (it holds every discarded attempt's narration), so
    `variant_count` exists to answer "how many attempts?" without fetching it.
    Writing the list anywhere else would let the two drift and the pager would
    lie about how many takes a turn has.
    """
    action.variants = entries
    action.variant_count = len(entries)


def variant_of(action: models.Action, adventure: models.Adventure) -> dict:
    """Freeze an action's *current* content as a variant entry.

    `adventure` supplies the resulting script/world state, so this must be
    called before any rollback — those live values are this attempt's outcome.
    """
    snapshot = action.context_snapshot if isinstance(action.context_snapshot, dict) else {}
    entry = {
        "text": action.text,
        "reasoning": action.reasoning,
        "script_state": snapshot_state(adventure),
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }
    for key in VARIANT_SNAPSHOT_KEYS:
        if key in snapshot:
            entry[key] = copy.deepcopy(snapshot[key])
    return entry


def apply_variant(action: models.Action, adventure: models.Adventure, index: int) -> None:
    """Make variant `index` the live one: its text onto the action, its
    outcome back onto the adventure."""
    entry = action.variants[index]
    action.text = entry.get("text", "")
    action.reasoning = entry.get("reasoning")
    snapshot = dict(action.context_snapshot) if isinstance(action.context_snapshot, dict) else {}
    for key in VARIANT_SNAPSHOT_KEYS:
        if key in entry:
            snapshot[key] = copy.deepcopy(entry[key])
        else:
            snapshot.pop(key, None)
    action.context_snapshot = snapshot
    action.world_delta = world_delta_of(snapshot)
    action.variant_index = index
    if isinstance(entry.get("script_state"), dict):
        adventure.script_state = copy.deepcopy(entry["script_state"])
    # The world state this attempt left behind lives inside its own snapshot
    # slice; absent for adventures with no RPG layer, where there's nothing to
    # restore anyway.
    world_state = (entry.get("world_state") or {}).get("state")
    if isinstance(world_state, dict):
        adventure.world_state = copy.deepcopy(world_state)


@router.patch("/{adventure_id}", response_model=schemas.AdventureOut)
def update_adventure(
    adventure_id: int,
    payload: schemas.AdventureUpdate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    adventure = get_adventure_or_404(adventure_id, db, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(adventure, field, value)
    db.commit()
    return adventure


@router.delete("/{adventure_id}", status_code=204)
def delete_adventure(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    adventure = get_adventure_or_404(adventure_id, db, user)
    db.delete(adventure)
    db.commit()


# ---------- Turn engine ----------

# One turn at a time per adventure (in-memory; fine for a single-process local app).
# Sync endpoints run in a threadpool, so the check-and-add must be guarded — and
# it must happen in the request phase, not when the SSE generator first runs,
# or two rapid requests both pass the check and generate concurrently.
_active_turns: set[int] = set()
_active_turns_guard = threading.Lock()


def acquire_turn_lock(adventure_id: int):
    """Atomically claim the adventure's turn slot; with_turn_lock releases it."""
    with _active_turns_guard:
        if adventure_id in _active_turns:
            raise HTTPException(409, "A turn is already generating for this adventure.")
        _active_turns.add(adventure_id)


async def with_turn_lock(adventure_id: int, gen):
    """Wrap an SSE generator so the lock (from acquire_turn_lock) is released."""
    try:
        async for event in gen:
            yield event
    finally:
        _active_turns.discard(adventure_id)


def format_player_input(action_type: str, text: str) -> str:
    """AI Dungeon input conventions."""
    text = text.strip()
    if action_type == "say":
        text = text.strip('"')
        if text and text[-1] not in ".!?…":
            text += "."
        return f'> You say "{text}"'
    if action_type == "do":
        if text.lower().startswith("you "):
            text = text[4:]
        if text and text[-1] not in ".!?…":
            text += "."
        return f"> You {text}"
    return text  # story: raw text appended


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# no-cache defeats any intermediary caching; X-Accel-Buffering makes
# nginx-style reverse proxies (hosted deploys) flush each event immediately
# instead of buffering the stream.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def action_json(action: models.Action) -> dict:
    return schemas.ActionOut.model_validate(action).model_dump(mode="json")


def next_index(adventure: models.Adventure) -> int:
    return context_history.max_action_index(adventure) + 1


def last_action(adventure: models.Adventure, db: Session) -> models.Action | None:
    """The newest action of any kind, or None. A query rather than
    `adventure.actions[-1]`, which would load the entire story to look at
    one row."""
    return (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adventure.id)
        .order_by(models.Action.index.desc())
        .first()
    )


async def generate_turn(
    adventure: models.Adventure,
    db: Session,
    pipeline: ScriptPipeline,
    user: models.User,
    retry_of: models.Action | None = None,
):
    """SSE generator: streams the AI continuation through the context/output
    script hooks, then stores the result.

    With `retry_of`, the result is appended as a new variant of that existing
    AI action rather than stored as a new one — the discarded attempt stays
    readable. The caller must have seeded `retry_of.variants` and rolled the
    adventure back first (see `retry_action`); if this generator ends without
    saving, that rollback is undone so state can't drift from the text still
    on screen."""
    saved = False
    try:
        async for event in _generate_turn(adventure, db, pipeline, user, retry_of):
            if event is _SAVED:
                saved = True
                continue
            yield event
    finally:
        if retry_of is not None and not saved:
            # Provider error, empty reply, a script stop, or the client hanging
            # up: put the attempt we rolled away from back in charge.
            apply_variant(retry_of, adventure, retry_of.variant_index)
            db.commit()


# Sentinel yielded by _generate_turn once the action is committed, so the
# wrapper above knows the rollback must stand rather than be reversed.
_SAVED = object()


async def _generate_turn(
    adventure: models.Adventure,
    db: Session,
    pipeline: ScriptPipeline,
    user: models.User,
    retry_of: models.Action | None = None,
):
    settings = get_settings(db, user)
    cfg = auth.resolve_provider_config(settings)
    # On a retry the row being regenerated is still attached to the adventure
    # (it carries the variant history), so it has to be filtered out of the
    # context — otherwise the model is shown the attempt it is replacing as if
    # it were established story, and writes a continuation of it.
    replacing_id = retry_of.id if retry_of is not None else None
    if cfg.using_demo:
        # No embedding/summarization calls on the server-funded key: memory
        # retrieval is skipped (with a visible note when the bank is on).
        memories = (
            {"used": [], "error": "Memory bank is unavailable on the shared demo key — add your own API key in Settings."}
            if adventure.memory_bank_enabled
            else None
        )
    else:
        memories = await memorybank.retrieve_memories(
            adventure, settings, update_stats=True, exclude_action_id=replacing_id
        )
    # Scoreboard as it stands before this AI turn's context/output hooks mutate
    # it — stapled onto the AI action so retry can start over from here.
    state_before = snapshot_state(adventure)
    world_state_before = snapshot_world_state(adventure)
    system_text, story_text, snapshot = build_context(
        adventure, settings, memories, exclude_action_id=replacing_id
    )

    # onModelContext: scripts see (and may rewrite) the whole assembled context.
    combined = f"{system_text}\n\n{story_text}" if system_text else story_text
    modified, stop = pipeline.run("context", combined)
    if stop:
        yield sse({"type": "stopped", "script": pipeline.report()})
        return
    context_changed = modified != combined
    parts = (
        PromptParts(system="", story=modified)
        if context_changed
        else PromptParts(system=system_text, story=story_text)
    )
    snapshot["script"] = pipeline.report() | {
        "context_changed": context_changed,
        "context_before": combined if context_changed else None,
        "context_after": modified if context_changed else None,
    }

    provider = OpenAICompatibleProvider(
        cfg.endpoint_url, cfg.api_key, cfg.model, settings.api_mode,
        settings.reasoning_max_tokens,
    )
    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    try:
        async for kind, chunk in provider.generate(
            parts, temperature=settings.temperature, max_tokens=settings.max_output_tokens
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
    # The model's literal reply, kept for the Insights "Raw AI output" view —
    # this still contains any world-state block before it gets stripped below.
    raw_output = text
    if not text:
        # If the model streamed reasoning but no story text, it spent its whole
        # budget thinking — say so instead of a mysterious "empty response".
        if reasoning_chunks:
            detail = (
                "The model used its entire token budget on reasoning and returned no "
                'story text. Raise "Max output tokens" in Settings, set a "Reasoning '
                'max tokens" cap, or switch to a non-reasoning model.'
            )
        else:
            detail = "The AI returned an empty response."
        yield sse({"type": "error", "detail": detail})
        return

    # onOutput
    text, _ = pipeline.run("output", text)
    if not text.strip():
        yield sse({"type": "error", "detail": "A script's output modifier returned empty text."})
        return
    snapshot["script"] = snapshot["script"] | pipeline.report()

    # RPG world state (Phase 12): pull the AI's state delta out of the reply,
    # let the engine referee it, and strip the block from the shown text.
    # A retry re-runs the *same* turn, so it keeps that turn's index — using
    # next_index here would advance the clock the cooldown rules run on.
    ai_index = retry_of.index if retry_of is not None else next_index(adventure)
    stat_schema = adventure.scenario.stat_schema if adventure.scenario else None
    if worldstate.has_schema(stat_schema):
        text, delta = worldstate.extract_delta(text)
        if not text.strip():
            yield sse({"type": "error", "detail": "The AI returned only a state update and no story text."})
            return
        new_world_state, ws_report = worldstate.apply_delta(
            adventure.world_state, stat_schema, delta, ai_index
        )
        adventure.world_state = new_world_state
        snapshot["world_state"] = {"delta": delta, "report": ws_report, "state": new_world_state}

    snapshot["raw_output"] = raw_output

    reasoning = "".join(reasoning_chunks).strip() or None
    if retry_of is not None:
        # Same row, one more attempt. Writing text/reasoning/snapshot through
        # the variant list keeps the row and its history in step.
        ai_action = retry_of
        ai_action.context_snapshot = snapshot
        history = list(ai_action.variants or [])
        history.append({
            "text": text,
            "reasoning": reasoning,
            "script_state": snapshot_state(adventure),
            "created_at": models.utcnow().isoformat(),
            **{k: copy.deepcopy(snapshot[k]) for k in VARIANT_SNAPSHOT_KEYS if k in snapshot},
        })
        set_variants(ai_action, history)
        apply_variant(ai_action, adventure, len(history) - 1)
    else:
        ai_action = models.Action(
            adventure_id=adventure.id,
            index=ai_index,
            type="ai",
            text=text,
            reasoning=reasoning,
            context_snapshot=snapshot,
            world_delta=world_delta_of(snapshot),
            state_before=state_before,
            world_state_before=world_state_before,
        )
        db.add(ai_action)
    adventure.updated_at = models.utcnow()
    if cfg.using_demo:
        # Successful demo turns count against the daily cap (checked up front
        # in the endpoint); failed provider calls above don't reach here.
        auth.count_demo_turn(user)
    db.commit()
    db.refresh(ai_action)
    yield _SAVED
    yield sse({"type": "done", "action": action_json(ai_action), "script": pipeline.report()})
    # Phase 6: fire-and-forget summarization/embedding (opens its own DB
    # session). Skipped on the demo key — background AI calls would be
    # unmetered spend on the server-funded key.
    if not cfg.using_demo:
        memorybank.schedule_post_turn(adventure)


def check_demo_cap(db: Session, user: models.User) -> None:
    """409/429-style guard before a turn starts, so a capped player's input
    isn't stored and then left without a reply."""
    settings = get_settings(db, user)
    if auth.resolve_provider_config(settings).using_demo and auth.demo_turns_left(user) <= 0:
        raise HTTPException(429, auth.DEMO_CAP_MESSAGE)


async def run_player_turn(
    adventure: models.Adventure,
    db: Session,
    payload: schemas.ActionCreate,
    user: models.User,
):
    pipeline = ScriptPipeline(adventure, db)

    # An empty do/say/story is just a continue.
    if payload.type != "continue" and payload.text.strip():
        # Scoreboard before the input hook mutates it — the pre-turn state that
        # undo restores to (the AI action keeps its own post-input snapshot).
        state_before = snapshot_state(adventure)
        world_state_before = snapshot_world_state(adventure)
        # onInput sees the formatted text (as in AI Dungeon: "> You ...").
        formatted = format_player_input(payload.type, payload.text)
        modified, stop = pipeline.run("input", formatted)
        if not modified.strip():
            yield sse({"type": "error", "detail": "A script's input modifier returned empty text.",
                       "script": pipeline.report()})
            return
        player_action = models.Action(
            adventure_id=adventure.id,
            index=next_index(adventure),
            type=payload.type,
            text=modified,
            state_before=state_before,
            world_state_before=world_state_before,
        )
        db.add(player_action)
        db.commit()
        db.refresh(player_action)
        # The new action was added via its FK, so the loaded adventure.actions
        # collection is stale — without this, build_context and next_index for
        # the AI action would not see the player action just saved.
        db.expire(adventure, ["actions"])
        yield sse({"type": "player", "action": action_json(player_action)})
        if stop:
            # onInput { stop: true } prevents the AI call.
            yield sse({"type": "stopped", "script": pipeline.report()})
            return

    async for event in generate_turn(adventure, db, pipeline, user):
        yield event


@router.post("/{adventure_id}/actions")
def create_action(
    adventure_id: int,
    payload: schemas.ActionCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.rate_limit("turn", request, user)
    limits.check_row_cap("actions", db, user, adventure=adventure)
    check_demo_cap(db, user)
    acquire_turn_lock(adventure_id)
    return StreamingResponse(
        with_turn_lock(adventure_id, run_player_turn(adventure, db, payload, user)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{adventure_id}/retry")
def retry_action(
    adventure_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Regenerate the last AI action, keeping the discarded attempt.

    The row survives: its current content is frozen as a variant, the shared
    script/world state rolls back to the pre-turn snapshot, and the new attempt
    is appended as the next variant. Nothing the AI wrote is ever thrown away.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.rate_limit("turn", request, user)
    check_demo_cap(db, user)
    acquire_turn_lock(adventure_id)
    last_ai = None
    try:
        newest = last_action(adventure, db)
        if newest is not None and newest.type == "ai":
            last_ai = newest
            # First retry: the row has no history yet, so record what's on
            # screen as variant 0 before anything is rolled back — the live
            # script/world state is precisely that attempt's outcome.
            if not last_ai.variant_count:
                set_variants(last_ai, [variant_of(last_ai, adventure)])
                last_ai.variant_index = 0
            # Roll the scoreboard back to before this AI turn's hooks ran, so
            # regenerating starts fresh instead of stacking output mutations on
            # top of the discarded attempt. NULL for pre-migration actions.
            if last_ai.state_before is not None:
                adventure.script_state = copy.deepcopy(last_ai.state_before)
            if last_ai.world_state_before is not None:
                adventure.world_state = copy.deepcopy(last_ai.world_state_before)
            db.commit()
            db.refresh(adventure)
    except BaseException:
        _active_turns.discard(adventure_id)
        raise
    return StreamingResponse(
        with_turn_lock(
            adventure_id,
            generate_turn(
                adventure, db, ScriptPipeline(adventure, db), user, retry_of=last_ai
            ),
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/{adventure_id}/actions/{action_id}/variants",
    response_model=list[schemas.VariantOut],
)
def list_variants(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Every attempt made for one AI turn. Fetched on demand — the adventure
    payload carries only the counts, so old narration doesn't ride along on
    every page load."""
    get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    variants = action.variants if isinstance(action.variants, list) else []
    return [
        schemas.VariantOut(
            index=i,
            text=entry.get("text", ""),
            reasoning=entry.get("reasoning"),
            created_at=entry.get("created_at"),
            active=(i == action.variant_index),
        )
        for i, entry in enumerate(variants)
    ]


@router.post(
    "/{adventure_id}/actions/{action_id}/variant", response_model=schemas.ActionOut
)
def select_variant(
    adventure_id: int,
    action_id: int,
    payload: schemas.VariantSelect,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Make an earlier attempt the live one again, restoring the script/world
    state it produced.

    Only the last action can be switched: the turns after an older one were
    written as a continuation of the text that's currently active, so swapping
    it out from underneath them would leave the story contradicting itself.
    Earlier turns' attempts stay readable through `list_variants`.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    variants = action.variants if isinstance(action.variants, list) else []
    if not 0 <= payload.index < len(variants):
        raise HTTPException(400, "No such attempt for this action")
    newest = last_action(adventure, db)
    if newest is None or newest.id != action.id:
        raise HTTPException(
            400,
            "Only the latest message can be switched — the story has already "
            "continued from this one.",
        )
    acquire_turn_lock(adventure_id)
    try:
        apply_variant(action, adventure, payload.index)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(action)
        return action
    finally:
        _active_turns.discard(adventure_id)


@router.post("/{adventure_id}/undo", response_model=list[schemas.ActionOut])
def undo_turn(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Delete the last turn: the trailing AI action plus its player action, if any.

    Also rolls the shared script_state back to before that turn ran and prunes
    any memory that summarized the removed actions. The turn lock guards against
    undoing while a turn is still generating."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    acquire_turn_lock(adventure_id)
    try:
        # Only the last turn is ever removed, so fetch the two actions it can
        # consist of rather than the whole story.
        newest = (
            db.query(models.Action)
            .filter(models.Action.adventure_id == adventure.id)
            .order_by(models.Action.index.desc())
            .limit(2)
            .all()
        )
        if not newest or newest[0].type == "start":
            raise HTTPException(400, "Nothing to undo")
        last = newest[0]
        preceding = newest[1] if len(newest) > 1 else None
        # The earliest action removed in this turn holds the pre-turn scoreboard.
        first_removed = last
        memorybank.note_action_removed(adventure, last)
        db.delete(last)
        if last.type == "ai" and preceding is not None and preceding.type in ("do", "say", "story"):
            first_removed = preceding
            memorybank.note_action_removed(adventure, first_removed)
            db.delete(first_removed)
        if first_removed.state_before is not None:
            adventure.script_state = copy.deepcopy(first_removed.state_before)
        if first_removed.world_state_before is not None:
            adventure.world_state = copy.deepcopy(first_removed.world_state_before)
        db.flush()  # apply deletes so pruning sees the shrunken action list
        db.expire(adventure, ["actions"])
        memorybank.prune_dangling_memories(adventure, db)
        db.commit()
        db.refresh(adventure)
        return adventure.actions
    finally:
        _active_turns.discard(adventure_id)


# ---------- Import / Export ----------

@router.get("/{adventure_id}/export")
def export_adventure(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Full backup: plot components, story cards, scripts (+state), every action."""
    adv = get_adventure_or_404(adventure_id, db, user)
    # Export is the one read that genuinely wants every attempt, so it asks for
    # the deferred `variants` column up front — iterating adv.actions instead
    # would lazy-load it one row at a time.
    exported_actions = (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adv.id)
        .options(undefer(models.Action.variants))
        .order_by(models.Action.index)
        .all()
    )
    return {
        "format": "ai-dnd-adventure-v1",
        "title": adv.title,
        "memory": adv.memory,
        "authorsNote": adv.authors_note,
        "aiInstructions": adv.ai_instructions,
        "storySummary": adv.story_summary,
        "scriptState": adv.script_state,
        "worldState": adv.world_state,
        "autoSummarize": adv.auto_summarize,
        "memoryBankEnabled": adv.memory_bank_enabled,
        "memoryCursor": adv.memory_cursor,
        "summaryCursor": adv.summary_cursor,
        "memories": [
            {
                "text": m.text, "pinned": m.pinned, "forgotten": m.forgotten,
                "sourceStart": m.source_start, "sourceEnd": m.source_end,
                "useCount": m.use_count,
            }
            for m in adv.memories
        ],
        "storyCards": [
            {"type": c.type, "name": c.name, "keys": c.keys, "entry": c.entry, "notes": c.notes}
            for c in adv.story_cards
        ],
        "scripts": [
            {
                "position": s.position, "enabled": s.enabled,
                "name": s.name, "description": s.description,
                "library": s.library_js, "input": s.input_js,
                "context": s.context_js, "output": s.output_js,
            }
            for s in adv.scripts
        ],
        "actions": [
            {
                "index": a.index, "type": a.type, "text": a.text,
                "reasoning": a.reasoning,
                # Retry history, narration only — a bundle carries no context
                # snapshots, so the per-attempt script/world state it would
                # restore isn't there to export either.
                "variants": [
                    {"text": v.get("text", ""), "reasoning": v.get("reasoning"),
                     "createdAt": v.get("created_at")}
                    for v in (a.variants or [])
                ] or None,
                "variantIndex": a.variant_index,
                "createdAt": a.created_at.isoformat(),
            }
            for a in exported_actions
        ],
    }


@router.post("/import", response_model=schemas.AdventureOut, status_code=201)
def import_adventure(
    request: Request,
    bundle: dict = Body(...),
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    if bundle.get("format") != "ai-dnd-adventure-v1":
        raise HTTPException(400, "Not an adventure export file (expected format ai-dnd-adventure-v1).")
    limits.rate_limit("import", request, user)
    limits.check_row_cap("adventures", db, user)
    limits.check_bundle_lists(
        story_cards=bundle.get("storyCards"),
        memories=bundle.get("memories"),
        actions=bundle.get("actions"),
    )

    # Raw-dict import bypasses the schemas — clamp strings headed for VARCHAR
    # columns (Postgres enforces the widths; see schemas.py).
    adventure = models.Adventure(
        user_id=user.id,
        title=str(bundle.get("title") or "Imported Adventure")[:schemas.NAME_MAX],
        memory=str(bundle.get("memory") or ""),
        authors_note=str(bundle.get("authorsNote") or ""),
        ai_instructions=str(bundle.get("aiInstructions") or ""),
        story_summary=str(bundle.get("storySummary") or ""),
        script_state=bundle.get("scriptState") or {},
        world_state=bundle.get("worldState") or {},
        auto_summarize=bool(bundle.get("autoSummarize", False)),
        memory_bank_enabled=bool(bundle.get("memoryBankEnabled", False)),
        memory_cursor=int(bundle.get("memoryCursor", 0)),
        summary_cursor=int(bundle.get("summaryCursor", 0)),
    )
    db.add(adventure)
    db.flush()

    for m in bundle.get("memories") or []:
        if isinstance(m, dict) and str(m.get("text") or "").strip():
            db.add(models.Memory(
                adventure_id=adventure.id,
                text=str(m["text"]),
                pinned=bool(m.get("pinned", False)),
                forgotten=bool(m.get("forgotten", False)),
                source_start=m.get("sourceStart"),
                source_end=m.get("sourceEnd"),
                use_count=int(m.get("useCount", 0)),
            ))

    for card in bundle.get("storyCards") or []:
        if isinstance(card, dict):
            db.add(models.StoryCard(
                adventure_id=adventure.id,
                type=str(card.get("type") or "")[:schemas.CARD_TYPE_MAX],
                name=str(card.get("name") or "")[:schemas.NAME_MAX],
                keys=str(card.get("keys") or ""),
                entry=str(card.get("entry") or ""),
                notes=str(card.get("notes") or ""),
            ))

    for i, s in enumerate(bundle.get("scripts") or []):
        if isinstance(s, dict):
            db.add(models.AdventureScript(
                adventure_id=adventure.id,
                position=int(s.get("position", i)),
                enabled=bool(s.get("enabled", True)),
                name=str(s.get("name") or "Imported Script")[:schemas.NAME_MAX],
                description=str(s.get("description") or ""),
                library_js=str(s.get("library") or ""),
                input_js=str(s.get("input") or ""),
                context_js=str(s.get("context") or ""),
                output_js=str(s.get("output") or ""),
            ))

    for i, a in enumerate(bundle.get("actions") or []):
        if isinstance(a, dict) and str(a.get("text") or ""):
            variants = [
                {"text": str(v.get("text") or ""), "reasoning": v.get("reasoning"),
                 "created_at": v.get("createdAt")}
                for v in (a.get("variants") or [])
                if isinstance(v, dict)
            ]
            db.add(models.Action(
                adventure_id=adventure.id,
                index=int(a.get("index", i)),
                type=str(a.get("type") or "story")[:20],  # VARCHAR(20)
                text=str(a["text"]),
                reasoning=str(a["reasoning"]) if a.get("reasoning") else None,
                variants=variants or None,
                variant_count=len(variants),
                # Clamped: a bundle could name an index its variant list
                # doesn't have, which would make the pager point at nothing.
                variant_index=min(max(int(a.get("variantIndex", 0)), 0), max(len(variants) - 1, 0)),
            ))

    db.commit()
    db.refresh(adventure)
    return adventure


# ---------- Adventure scripts ----------

# Fields copied from a library Script into its adventure-script snapshot, and
# compared to decide whether a copy is out of date.
SYNC_FIELDS = ("name", "description", "library_js", "input_js", "context_js", "output_js")


def resolve_library_script(
    adv_script: models.AdventureScript, db: Session, user: models.User
) -> models.Script | None:
    """The player-owned library Script an adventure-script can re-sync from:
    the one it was copied from, or — for legacy copies with no link — one of
    the player's own scripts sharing its name. Only the player's own scripts
    are ever considered, so a demo-derived copy has nothing to sync to."""
    if adv_script.source_script_id is not None:
        script = db.get(models.Script, adv_script.source_script_id)
        if script is not None and script.user_id == user.id:
            return script
    return (
        db.query(models.Script)
        .filter(models.Script.user_id == user.id, models.Script.name == adv_script.name)
        .order_by(models.Script.updated_at.desc())
        .first()
    )


def _mark_out_of_date(
    adv_script: models.AdventureScript, db: Session, user: models.User
) -> models.AdventureScript:
    """Attach a transient `out_of_date` flag (read by AdventureScriptOut):
    True/False when a syncable library version exists, None when it doesn't."""
    library = resolve_library_script(adv_script, db, user)
    adv_script.out_of_date = (
        None if library is None
        else any(getattr(adv_script, f) != getattr(library, f) for f in SYNC_FIELDS)
    )
    return adv_script


@router.get("/{adventure_id}/scripts", response_model=list[schemas.AdventureScriptOut])
def list_adventure_scripts(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    adventure = get_adventure_or_404(adventure_id, db, user)
    return [_mark_out_of_date(s, db, user) for s in adventure.scripts]


@router.post(
    "/{adventure_id}/scripts/{adv_script_id}/sync",
    response_model=schemas.AdventureScriptOut,
)
def sync_adventure_script(
    adventure_id: int,
    adv_script_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Overwrite this copy's code with the latest from its library script,
    keeping `enabled`, `position`, and the adventure's shared script_state."""
    get_adventure_or_404(adventure_id, db, user)
    script = db.get(models.AdventureScript, adv_script_id)
    if script is None or script.adventure_id != adventure_id:
        raise HTTPException(404, "Script not found")
    library = resolve_library_script(script, db, user)
    if library is None:
        raise HTTPException(404, "No library script to sync from")
    for field in SYNC_FIELDS:
        setattr(script, field, getattr(library, field))
    # Adopt the link so a name-matched legacy copy syncs by id next time.
    script.source_script_id = library.id
    db.commit()
    db.refresh(script)
    return _mark_out_of_date(script, db, user)


@router.patch(
    "/{adventure_id}/scripts/{adv_script_id}", response_model=schemas.AdventureScriptOut
)
def update_adventure_script(
    adventure_id: int,
    adv_script_id: int,
    payload: schemas.AdventureScriptUpdate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    script = db.get(models.AdventureScript, adv_script_id)
    if script is None or script.adventure_id != adventure_id:
        raise HTTPException(404, "Script not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(script, field, value)
    db.commit()
    return script


# ---------- Refresh from scenario ----------
#
# An adventure copies the scenario's plot text and story cards at creation so
# later authoring never disturbs a story in progress (same reasoning as the
# per-script "Sync from library" above). This is the explicit opt-out: pull the
# scenario's current content back down over the copy.


def resolve_source_scenario(
    adventure: models.Adventure, db: Session, user: models.User
) -> models.Scenario | None:
    """The scenario an adventure can refresh from — the one it was started from,
    if it still exists and is still readable (own, or a shared demo one). None
    once the scenario is deleted (scenario_id goes NULL) or was unshared."""
    if adventure.scenario_id is None:
        return None
    scenario = db.get(models.Scenario, adventure.scenario_id)
    if scenario is None or (scenario.user_id != user.id and not scenario.is_public):
        return None
    return scenario


def _placeholder_names(*texts: str) -> list[str]:
    """Unique ${Placeholder} names across the given texts, first appearance first
    (mirrors the frontend's extractPlaceholders)."""
    names: list[str] = []
    for text in texts:
        for match in PLACEHOLDER_RE.finditer(text or ""):
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
    return names


def scenario_placeholder_names(scenario: models.Scenario) -> list[str]:
    """Every placeholder the scenario's *refreshable* content asks for. The
    opening prompt is excluded — a refresh never rewrites it."""
    texts = [scenario.memory, scenario.authors_note, scenario.ai_instructions]
    for card in scenario.story_cards:
        texts += [card.keys, card.entry]
    for ndef in (scenario.stat_schema or {}).get("npcs", {}).values():
        if isinstance(ndef, dict):
            texts += [str(ndef.get("keys") or ""), str(ndef.get("desc") or "")]
    return _placeholder_names(*texts)


def _scenario_cards(adventure: models.Adventure) -> dict[str, models.StoryCard]:
    """The adventure's scenario-derived cards, keyed by source_ref.

    Adventures created before `source_ref` existed have none, so those fall back
    to matching the scenario's cards by name — but only when the adventure has no
    tagged cards at all, otherwise a player-authored card that happens to share a
    scenario card's name would be adopted and overwritten.
    """
    return {c.source_ref: c for c in adventure.story_cards if c.source_ref}


def _match_legacy(
    adventure: models.Adventure, specs: dict[str, dict]
) -> dict[str, models.StoryCard]:
    by_name: dict[str, models.StoryCard] = {}
    for card in adventure.story_cards:
        by_name.setdefault((card.name or "").strip().lower(), card)
    matched: dict[str, models.StoryCard] = {}
    for ref, spec in specs.items():
        card = by_name.get((spec["name"] or "").strip().lower())
        if card is not None:
            matched[ref] = card
    return matched


def plan_refresh(
    adventure: models.Adventure, scenario: models.Scenario, values: dict[str, str]
) -> tuple[dict, dict, dict]:
    """Work out what a refresh would change, without touching anything.

    Returns (plan, specs, matched) — `plan` is the UI-facing summary, `specs` the
    scenario's card specs by ref, `matched` the existing adventure card for each
    ref that already has one.
    """
    fields = {
        field: {"old": getattr(adventure, field), "new": fill_placeholders(
            getattr(scenario, field), values)}
        for field in SCENARIO_TEXT_FIELDS
    }
    changed_fields = {f: v for f, v in fields.items() if v["old"] != v["new"]}

    specs = scenario_card_specs(scenario, values)
    tagged = _scenario_cards(adventure)
    matched = tagged or _match_legacy(adventure, specs)

    added, updated = [], []
    for ref, spec in specs.items():
        card = matched.get(ref)
        if card is None:
            added.append(spec["name"])
        elif any(getattr(card, f) != spec[f] for f in CARD_FIELDS):
            updated.append(card.name or spec["name"])
    # Only cards the scenario is known to have produced are removable; a
    # player-authored card (no source_ref) is never touched.
    removed = [c.name for ref, c in tagged.items() if ref not in specs]

    _, world = worldstate.reconcile(adventure.world_state, scenario.stat_schema)

    plan = {
        "scenario_id": scenario.id,
        "scenario_title": scenario.title,
        "fields": changed_fields,
        "cards": {"added": added, "updated": updated, "removed": removed},
        "world_state": world,
    }
    plan["has_changes"] = bool(
        changed_fields or added or updated or removed
        or world["added"] or world["removed"]
    )
    return plan, specs, matched


@router.get("/{adventure_id}/refresh", response_model=schemas.RefreshPlan)
def preview_refresh(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """What "Update from scenario" would change, for the confirm dialog."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    scenario = resolve_source_scenario(adventure, db, user)
    if scenario is None:
        raise HTTPException(404, "No scenario to update from")
    stored = adventure.placeholders if isinstance(adventure.placeholders, dict) else {}
    plan, _, _ = plan_refresh(adventure, scenario, stored)
    # Adventures started before placeholder answers were stored have none, and an
    # author can add a new ${...} after the fact — either way the player is asked
    # for the missing ones, and the answers are saved for next time.
    plan["placeholders_needed"] = [
        n for n in scenario_placeholder_names(scenario) if n not in stored
    ]
    return plan


@router.post("/{adventure_id}/refresh", response_model=schemas.AdventureOut)
def refresh_from_scenario(
    adventure_id: int,
    payload: schemas.AdventureRefresh = Body(default=schemas.AdventureRefresh()),
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Pull the scenario's current plot text, story cards and stat schema down
    over this adventure's copy.

    Overwrites the plot fields and every scenario-derived card, adds what the
    scenario gained and removes what it dropped. Deliberately left alone: the
    opening `start` action (the story is built on it, and it is baked into
    memories and the summary), the adventure's own title, its story summary, its
    player-authored story cards, and — via `worldstate.reconcile` — the live
    value of every stat the schema still defines.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    scenario = resolve_source_scenario(adventure, db, user)
    if scenario is None:
        raise HTTPException(404, "No scenario to update from")

    values = {**(adventure.placeholders if isinstance(adventure.placeholders, dict) else {}),
              **payload.placeholders}

    # A refresh rewrites the same state a turn is mid-way through mutating, so it
    # takes the turn slot rather than racing the generator.
    acquire_turn_lock(adventure_id)
    try:
        _, specs, matched = plan_refresh(adventure, scenario, values)

        for field in SCENARIO_TEXT_FIELDS:
            setattr(adventure, field, fill_placeholders(getattr(scenario, field), values))

        for ref, spec in specs.items():
            card = matched.get(ref)
            if card is None:
                db.add(models.StoryCard(adventure_id=adventure.id, source_ref=ref, **spec))
                continue
            for field in CARD_FIELDS:
                setattr(card, field, spec[field])
            # Adopt the ref so a name-matched legacy card syncs by id next time.
            card.source_ref = ref
        for ref, card in _scenario_cards(adventure).items():
            if ref not in specs:
                db.delete(card)

        adventure.world_state, _ = worldstate.reconcile(
            adventure.world_state, scenario.stat_schema
        )
        adventure.placeholders = values
        db.commit()
    finally:
        _active_turns.discard(adventure_id)

    db.refresh(adventure)
    return adventure


# ---------- Insights ----------

@router.get("/{adventure_id}/context")
async def dry_run_context(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """What would be sent to the AI if the player continued right now."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    settings = get_settings(db, user)
    if auth.resolve_provider_config(settings).using_demo:
        memories = (
            {"used": [], "error": "Memory bank is unavailable on the shared demo key."}
            if adventure.memory_bank_enabled
            else None
        )
    else:
        memories = await memorybank.retrieve_memories(adventure, settings, update_stats=False)
    _, _, report = build_context(adventure, settings, memories)
    return report


@router.get("/{adventure_id}/actions/{action_id}/context")
def action_context(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    if action.context_snapshot is None:
        raise HTTPException(404, "No context snapshot for this action")
    return action.context_snapshot


# ---------- Memory bank (Phase 6) ----------

@router.get("/{adventure_id}/memories", response_model=list[schemas.MemoryOut])
def list_memories(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    return get_adventure_or_404(adventure_id, db, user).memories


@router.post("/{adventure_id}/memories", response_model=schemas.MemoryOut, status_code=201)
def create_memory(
    adventure_id: int,
    payload: schemas.MemoryCreate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Manually add a memory; it gets embedded by the next post-turn pass."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.check_row_cap("memories", db, user, adventure=adventure)
    if not payload.text.strip():
        raise HTTPException(400, "Memory text cannot be empty")
    memory = models.Memory(adventure_id=adventure.id, text=payload.text.strip())
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


@router.patch("/{adventure_id}/memories/{memory_id}", response_model=schemas.MemoryOut)
def update_memory(
    adventure_id: int,
    memory_id: int,
    payload: schemas.MemoryUpdate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    memory = db.get(models.Memory, memory_id)
    if memory is None or memory.adventure_id != adventure_id:
        raise HTTPException(404, "Memory not found")
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if "text" in fields and fields["text"].strip() != memory.text:
        memorybank.set_vector(memory, None)  # re-embed on the next post-turn pass
    for field, value in fields.items():
        setattr(memory, field, value)
    db.commit()
    return memory


@router.delete("/{adventure_id}/memories/{memory_id}", status_code=204)
def delete_memory(
    adventure_id: int,
    memory_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    memory = db.get(models.Memory, memory_id)
    if memory is None or memory.adventure_id != adventure_id:
        raise HTTPException(404, "Memory not found")
    db.delete(memory)
    db.commit()


# ---------- Actions (CRUD) ----------

@router.get("/{adventure_id}/actions", response_model=list[schemas.ActionOut])
def list_actions(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    get_adventure_or_404(adventure_id, db, user)
    return (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adventure_id)
        .order_by(models.Action.index)
        .all()
    )


@router.patch("/{adventure_id}/actions/{action_id}", response_model=schemas.ActionOut)
def update_action(
    adventure_id: int,
    action_id: int,
    payload: schemas.ActionUpdate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    action.text = payload.text
    # Keep the live variant in step, or paging away and back would silently
    # revert the edit.
    if action.variant_count:
        variants = action.variants if isinstance(action.variants, list) else []
        if 0 <= action.variant_index < len(variants):
            history = copy.deepcopy(variants)
            history[action.variant_index]["text"] = payload.text
            set_variants(action, history)
    db.commit()
    return action


@router.delete("/{adventure_id}/actions/{action_id}", status_code=204)
def delete_action(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    adventure = get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    # Cursor bookkeeping, same as undo: slide the cursors down if this action
    # sits before them, then drop any memory left describing a deleted action.
    memorybank.note_action_removed(adventure, action)
    db.delete(action)
    db.flush()  # apply the delete so pruning sees the shrunken action list
    db.expire(adventure, ["actions"])
    memorybank.prune_dangling_memories(adventure, db)
    db.commit()
