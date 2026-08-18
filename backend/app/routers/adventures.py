import json
import re
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, load_only, undefer
from sqlalchemy.orm.attributes import set_committed_value

from .. import (
    attempts, auth, bundle, images, limits, memorybank, models, schemas, tree,
    worldstate,
)
from ..context import build_context, cursors
from ..context import history as context_history
from ..context import lineage
from ..database import get_db
from ..providers import OpenAICompatibleProvider, PromptParts, ProviderError
from ..scripting import ScriptPipeline
from .settings import get_settings

router = APIRouter(prefix="/api/adventures", tags=["adventures"])

CurrentUser = Depends(auth.get_current_user)

# Exactly what schemas.ActionOut renders, named rather than implied.
#
# `deferred=True` in models.py already keeps the four heavy columns out of a
# bulk read, but it makes narrowness the default that a *future* column has to
# remember to ask for — and both egress blowouts this project has had were a
# column nobody remembered. Listing what a list response carries inverts that:
# a new column costs nothing here until someone adds it to this tuple.
#
# `world_delta` is on the list because ActionOut.world_changes is computed from
# it. Leaving it off would not save the bytes, it would spend them one row at a
# time as a lazy load, which is worse.
ACTION_LIST_COLUMNS = (
    models.Action.adventure_id,
    models.Action.index,
    models.Action.type,
    models.Action.text,
    models.Action.reasoning,
    models.Action.world_delta,
    models.Action.variant_count,
    models.Action.variant_index,
    models.Action.created_at,
)

# How many actions an adventure opens with, and how many arrive per scroll.
#
# Opening a finished adventure used to fetch the whole story in one response —
# 589.5 kB on production's longest, and growing, because a story only ever gets
# longer. 60 is a few screens of reading: enough that the common case (open,
# read the end, take a turn) never pages at all, small enough that the worst
# case is bounded by the window rather than by the story.
ACTION_PAGE = 60


def action_window(
    db: Session,
    adventure: models.Adventure,
    before_id: int | None = None,
    limit: int = ACTION_PAGE,
) -> tuple[list[models.Action], int, bool]:
    """The `limit` actions immediately older than `before_id`, oldest first.

    Returns (actions, total, has_more). `before_id=None` is the newest window.

    Scoped to the story being played — the head branch's lineage — rather than
    to the adventure, so a sibling branch's turns can never appear in the
    transcript. `total` counts the same path, because it is what tells the
    reader there is more above.

    Anchored on an action, not on a count, and never on arithmetic over depth.
    Two separate reasons, and both bite:

    * **Appends.** Counting back from the newest means every older position
      shifts when a turn lands. A reader who scrolls up while a turn is
      generating would be handed a window one row out — re-sending one action
      and silently skipping another. An anchor is fixed: "older than this one"
      means the same thing before and after the story grows.
    * **The story tree.** Depth is dense today and branching ends that.
      Comparing depths to order a path survives; treating them as positions
      does not.

    `has_more` comes from asking for one row past the window rather than from
    counting, so it costs a row and not a scan.
    """
    path = lineage.path_of(db, adventure)
    on_path = (
        models.Action.adventure_id == adventure.id,
        path.clause(models.Action),
    )
    total = db.query(func.count(models.Action.id)).filter(*on_path).scalar()
    if limit <= 0:
        return [], total, total > 0

    query = db.query(models.Action).options(load_only(*ACTION_LIST_COLUMNS)).filter(*on_path)
    if before_id is not None:
        anchor = (
            db.query(models.Action.depth)
            .filter(models.Action.id == before_id, *on_path)
            .scalar()
        )
        if anchor is None:
            # The anchor was deleted (undo, or a turn edited away) while the
            # reader was scrolling, or it belongs to a story this branch is not
            # on. Nothing older can be identified relative to a row that is not
            # here, so report the end rather than guessing and handing back a
            # duplicate page.
            return [], total, False
        query = query.filter(models.Action.depth < anchor)

    rows = (
        query.order_by(models.Action.depth.desc(), models.Action.id.desc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, total, has_more


# Exactly what schemas.MemoryOut renders. `embedded` is a real column and is on
# the list; the vector it describes is not, and must never be.
MEMORY_LIST_COLUMNS = (
    models.Memory.adventure_id,
    models.Memory.text,
    models.Memory.pinned,
    models.Memory.forgotten,
    models.Memory.embedded,
    models.Memory.use_count,
    models.Memory.last_used_at,
    models.Memory.source_start,
    models.Memory.source_end,
    models.Memory.created_at,
)


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


def _latest_narration(db: Session, head_branches: dict[int, int | None]) -> dict[int, str]:
    """Map adventure id -> text of its most recent narrated action.

    One window-function query rather than a per-adventure lookup, so the list
    endpoint stays at a fixed number of round trips.

    Scoped by head *branch* rather than by the full lineage, which is the one
    place in the codebase that is allowed to be: a lineage clause per adventure
    would put a hundred OR-terms on the index screen's query to pick one row
    each. The two answers differ only for a branch with no nodes of its own,
    and a branch is created by playing a turn onto it, so that state does not
    exist. An adventure with no branch at all has no story to quote.
    """
    branch_ids = [b for b in head_branches.values() if b is not None]
    if not branch_ids:
        return {}
    ranked = (
        db.query(
            models.Action.adventure_id.label("adventure_id"),
            models.Action.text.label("text"),
            func.row_number()
            .over(
                partition_by=models.Action.adventure_id,
                order_by=(models.Action.depth.desc(), models.Action.id.desc()),
            )
            .label("rank"),
        )
        .filter(
            models.Action.adventure_id.in_(list(head_branches)),
            models.Action.branch_id.in_(branch_ids),
            models.Action.type.in_(NARRATION_TYPES),
        )
        .subquery()
    )
    rows = db.query(ranked.c.adventure_id, ranked.c.text).filter(ranked.c.rank == 1).all()
    return {adventure_id: text for adventure_id, text in rows}


@router.get("", response_model=list[schemas.AdventureListItem])
def list_adventures(db: Session = Depends(get_db), user: models.User = CurrentUser):
    # Four columns of Adventure, named, rather than the entity. The entity is
    # sixteen columns wide and carries script_state, world_state, placeholders,
    # story_summary, memory, authors_note and ai_instructions — ~15 kB a row in
    # production, none of it on this screen, all of it fetched once per
    # adventure every time the index loads. Naming the columns also means the
    # next wide column added to Adventure has to opt *in* to being listed here.
    rows = (
        db.query(
            models.Adventure.id,
            models.Adventure.scenario_id,
            models.Adventure.title,
            models.Adventure.updated_at,
            models.Adventure.head_branch_id,
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
    narration = _latest_narration(db, {row[0]: row[4] for row in rows})
    return [
        schemas.AdventureListItem(
            id=adv_id,
            scenario_id=scenario_id,
            scenario_title=scenario_title,
            title=title,
            updated_at=updated_at,
            action_count=count,
            snippet=_snippet(narration.get(adv_id, "")),
            # The art belongs to the scenario, so the cache-busting stamp is the
            # scenario's updated_at, not the adventure's.
            image_url=images.public_url(scenario_id, image or "", scenario_updated),
            icon=icon or "",
        )
        # `count` is every action of the adventure, not of the path. Under one
        # branch they are the same number; once forking ships the index screen
        # will overstate a story that has siblings hanging off it, and the fix
        # belongs with SP5, where a fork can first exist.
        for (adv_id, scenario_id, title, updated_at, _head_branch_id, count,
             scenario_title, image, icon, scenario_updated) in rows
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
    # Every adventure has a story tree from the moment it exists, even before
    # anything is played onto it — an adventure with a NULL head is a state the
    # tree would otherwise have to tolerate everywhere for no gain.
    tree.head_branch(db, adventure)

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
            opening = models.Action(
                adventure_id=adventure.id,
                index=0,
                type="start",
                text=fill_placeholders(scenario.prompt, values),
            )
            # The opening node leaves behind the state the adventure starts
            # with, so undoing or retrying the first turn has somewhere to
            # roll back to.
            attempts.snapshot_outcome(adventure, opening)
            tree.place_action(db, adventure, opening)
            db.add(opening)

    db.commit()
    db.refresh(adventure)
    return adventure


@router.get("/{adventure_id}", response_model=schemas.AdventureOut)
def get_adventure(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """The adventure, and the newest window of its story.

    `actions` is the last ACTION_PAGE, not all of them; `action_count` says how
    many there are so the reader knows there is more above. Older pages come
    from GET /{id}/actions as they scroll up.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    actions, total, _ = action_window(db, adventure)
    # Hand the response the window as if the relationship had loaded it.
    # `set_committed_value` is the only way to do this safely: assigning
    # `adventure.actions = [...]` marks the collection dirty, and the
    # relationship cascades delete-orphan, so the actions left out of the
    # window would be deleted on the next flush. This records them as the
    # loaded, unmodified value instead, so serialising touches no lazy load
    # and nothing is pending.
    set_committed_value(adventure, "actions", actions)
    out = schemas.AdventureOut.model_validate(adventure)
    out.action_count = total
    return out


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


# ---------- Retry history (sibling attempts) ----------
#
# Retry used to delete the AI action and generate a replacement, then kept the
# row and pushed each attempt into a JSON list on it. Now every attempt is its
# own node: same branch, same depth, one of them `live`. `app/attempts.py` owns
# the group and both of its invariants; the endpoints below only ask it things.


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
    # Nothing else would ever ask for this adventure's vectors again, so the
    # cache would hold them until the process restarted.
    memorybank.forget_cached_vectors(adventure_id)


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


def next_depth(adventure: models.Adventure) -> int:
    """Where the next node played onto this story goes: one past the tip.

    Not `next_index`, which the two agreed on until SP5. `index` has to stay
    unique across the whole adventure — it is the v1 bundle's key — so on a
    story forked at depth 6 after twenty turns it would hand the next node
    depth 21 and leave a fourteen-deep hole in the middle of a path. A depth is
    a position along *this* story, and the branch is what makes it unambiguous.
    """
    return adventure.head_depth + 1


def last_action(adventure: models.Adventure, db: Session) -> models.Action | None:
    """The newest action of any kind on the story being played, or None.

    A query rather than `adventure.actions[-1]`, which would load the entire
    story to look at one row — and, since that collection is every branch's
    actions, would sometimes look at the wrong one.
    """
    return (
        db.query(models.Action)
        .filter(
            models.Action.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Action),
        )
        .order_by(models.Action.depth.desc(), models.Action.id.desc())
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

    With `retry_of`, the result is stored as a *sibling* of that AI action —
    same turn, same coordinate, another take — and the discarded attempt stays
    exactly where it was written. The caller must have rolled the adventure
    back to before the turn first (see `retry_action`); if this generator ends
    without saving, that rollback is undone so state can't drift from the text
    still on screen."""
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
            # up: no sibling was written, so the attempt on screen is still the
            # live one — put back the state it produced.
            attempts.restore_state(adventure, retry_of)
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
    # On a retry the attempt being replaced is still the live node of its turn
    # — it stays live until a replacement exists to take over — so it has to be
    # filtered out of the context, or the model is shown the attempt it is
    # supposed to be replacing as established story and writes a sequel to it.
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
    # A retry re-runs the *same* turn, so it is played at that turn's depth —
    # the clock the cooldown rules run on is a position in the story, and a
    # second take on turn 12 is still turn 12. (It was `retry_of.index` until
    # SP4, which held the same number; depth is the one that stays true once a
    # branch has its own numbering.)
    ai_depth = retry_of.depth if retry_of is not None else next_depth(adventure)
    stat_schema = adventure.scenario.stat_schema if adventure.scenario else None
    if worldstate.has_schema(stat_schema):
        text, delta = worldstate.extract_delta(text)
        if not text.strip():
            yield sse({"type": "error", "detail": "The AI returned only a state update and no story text."})
            return
        new_world_state, ws_report = worldstate.apply_delta(
            adventure.world_state, stat_schema, delta, ai_depth
        )
        adventure.world_state = new_world_state
        snapshot["world_state"] = {"delta": delta, "report": ws_report, "state": new_world_state}

    snapshot["raw_output"] = raw_output

    reasoning = "".join(reasoning_chunks).strip() or None
    ai_action = models.Action(
        adventure_id=adventure.id,
        # A sibling shares the turn's legacy index for the same reason it
        # shares its depth: it is the same turn. Two rows then hold one index,
        # which `max_action_index` (a maximum, not a count) survives, and
        # nothing else still reads the column.
        index=retry_of.index if retry_of is not None else next_index(adventure),
        depth=ai_depth,
        type="ai",
        text=text,
        reasoning=reasoning,
        context_snapshot=snapshot,
        world_delta=world_delta_of(snapshot),
    )
    attempts.snapshot_outcome(adventure, ai_action)
    if retry_of is not None:
        attempts.add_attempt(db, adventure, retry_of, ai_action)
        db.add(ai_action)
        # The text at this coordinate has just changed, so whatever was derived
        # from it is no longer about the story: withdraw the memory hanging off
        # the node and hand the ground back to both passes. Before SP4 this was
        # unreachable, because the summarizer held the newest action back until
        # a turn had landed on top of it — see `memorybank`.
        memorybank.forget_node(db, adventure, retry_of)
        cursors.rewind_all(adventure, retry_of.branch_id, ai_depth - 1)
        db.flush()
        attempts.renumber(attempts.group(db, ai_action))
    else:
        tree.place_action(db, adventure, ai_action)
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
            depth=next_depth(adventure),
            type=payload.type,
            text=modified,
        )
        # The scoreboard once the input hook has run — what this node left
        # behind, which is where the AI turn after it starts and where a retry
        # of that turn rolls back to.
        attempts.snapshot_outcome(adventure, player_action)
        tree.place_action(db, adventure, player_action)
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

    The attempt on screen is left exactly as it was written; the shared
    script/world state rolls back to what the node in front of it left behind,
    and the new take is stored as a sibling at the same coordinate. Nothing the
    AI wrote is ever rewritten, let alone thrown away.
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
            # Roll the scoreboard back to before this AI turn's hooks ran, so
            # regenerating starts fresh instead of stacking output mutations on
            # top of the attempt being replaced. A no-op where the preceding
            # node has no snapshot (a row written before SP4 the migration
            # could not derive one for), which leaves the state alone rather
            # than resetting it.
            attempts.roll_back_before(db, adventure, last_ai)
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
    every page load.

    Addressed by *any* attempt at the turn, not only the live one: switching
    changes which row the story tells, and a client holding the id it was given
    a moment ago must still be able to ask about the same turn.
    """
    get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    rows = attempts.group(db, action)
    if len(rows) < 2:
        return []  # never retried: the turn is its own only take
    return [
        schemas.VariantOut(
            index=i,
            text=row.text,
            reasoning=row.reasoning,
            created_at=row.created_at.isoformat() if row.created_at else None,
            active=row.live,
        )
        for i, row in enumerate(rows)
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
    rows = attempts.group(db, action)
    if not 0 <= payload.index < len(rows) or len(rows) < 2:
        raise HTTPException(400, "No such attempt for this action")
    newest = last_action(adventure, db)
    if newest is None or newest.depth != action.depth or newest.branch_id != action.branch_id:
        raise HTTPException(
            400,
            "Only the latest message can be switched — the story has already "
            "continued from this one.",
        )
    acquire_turn_lock(adventure_id)
    try:
        chosen = rows[payload.index]
        if not chosen.live:
            # The story at this coordinate is about to say something else, so
            # anything derived from what it used to say is withdrawn — the same
            # move a retry makes, for the same reason.
            memorybank.forget_node(db, adventure, chosen)
            cursors.rewind_all(adventure, chosen.branch_id, (chosen.depth or 0) - 1)
        attempts.make_live(db, adventure, chosen)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(chosen)
        # The row that answers is the one now in the story, which is a
        # *different row* from the one addressed — that is the whole change: an
        # attempt is a node, so choosing one moves the story onto it rather
        # than rewriting anything.
        return chosen
    finally:
        _active_turns.discard(adventure_id)


def delete_turn(
    db: Session, adventure: models.Adventure, node: models.Action
) -> None:
    """Remove a turn: every attempt at it, not only the one on screen.

    A discarded attempt is a leaf hanging off the same coordinate, and it is
    only reachable *through* that coordinate — leaving it behind when the turn
    goes would leave a row nothing can name and no read can see. Whatever the
    turn produced is withdrawn once, because a memory hangs off the coordinate
    rather than off one of its attempts.
    """
    memorybank.forget_node(db, adventure, node)
    for attempt in attempts.group(db, node):
        db.delete(attempt)


# ---------- Branches (Phase 14, SP5) ----------
#
# Attempts pile up at the tip as siblings and cost nothing. One becomes a
# *branch* at the moment the player takes the story down it and leaves the line
# that moved past it — which is the same event as "a turn is played past it",
# seen from the side that has to do the work. Doing it here rather than on the
# next turn means a branch is only ever created for a divergence somebody
# actually built on, and the line being left is never disturbed.


def current_window(db: Session, adventure: models.Adventure) -> schemas.ActionPage:
    actions, total, has_more = action_window(db, adventure)
    return schemas.ActionPage(
        actions=[schemas.ActionOut.model_validate(a) for a in actions],
        total=total,
        has_more=has_more,
    )


@router.get("/{adventure_id}/branches", response_model=list[schemas.BranchOut])
def list_branches(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Every branch of the adventure, with where each one leaves its parent.

    The shape a tree view is drawn from: `fork_depth` says where the line
    splits off and `depth` where it currently ends, so the whole picture is one
    query over `branches` plus one grouped query over `actions` — never one per
    branch, which is how a spatial view of a hundred forks stops being a
    hundred round trips.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    branches = (
        db.query(models.Branch)
        .filter(models.Branch.adventure_id == adventure.id)
        .order_by(models.Branch.id)
        .all()
    )
    owned = {
        branch_id: (count, tip)
        for branch_id, count, tip in db.query(
            models.Action.branch_id,
            func.count(models.Action.id),
            func.max(models.Action.depth),
        )
        .filter(
            models.Action.adventure_id == adventure.id,
            models.Action.live.is_(True),
        )
        .group_by(models.Action.branch_id)
        .all()
    }
    out = []
    for branch in branches:
        count, tip = owned.get(branch.id, (0, None))
        out.append(schemas.BranchOut(
            id=branch.id,
            parent_branch_id=branch.parent_branch_id,
            fork_depth=branch.fork_depth,
            # A branch with nothing of its own sits at its fork point: that is
            # the last node its story contains, borrowed but the tip all the
            # same. Mirrors tree.refresh_head.
            depth=tip if tip is not None else (
                branch.fork_depth if branch.fork_depth is not None else tree.NO_DEPTH
            ),
            own_actions=count,
            is_head=(branch.id == adventure.head_branch_id),
            name=branch.name,
            created_at=branch.created_at,
        ))
    return out


def get_branch_or_404(
    adventure: models.Adventure, branch_id: int, db: Session
) -> models.Branch:
    """One branch of this adventure, or a 404 that does not confirm it exists."""
    branch = db.get(models.Branch, branch_id)
    if branch is None or branch.adventure_id != adventure.id:
        raise HTTPException(404, "Branch not found")
    return branch


@router.patch(
    "/{adventure_id}/branches/{branch_id}", response_model=schemas.BranchOut
)
def rename_branch(
    adventure_id: int,
    branch_id: int,
    payload: schemas.BranchRename,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Name a branch, or clear the name to leave it unnamed again.

    A blank string means the same thing as `null` — a name of spaces is not a
    name anyone chose, and storing one would give the client something to draw
    that reads as an empty label rather than as a fork depth.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    branch = get_branch_or_404(adventure, branch_id, db)
    name = (payload.name or "").strip()
    branch.name = name or None
    adventure.updated_at = models.utcnow()
    db.commit()
    db.refresh(branch)
    tip = (
        db.query(func.max(models.Action.depth))
        .filter(
            models.Action.adventure_id == adventure.id,
            models.Action.branch_id == branch.id,
            models.Action.live.is_(True),
        )
        .scalar()
    )
    return schemas.BranchOut(
        id=branch.id,
        parent_branch_id=branch.parent_branch_id,
        fork_depth=branch.fork_depth,
        depth=tip if tip is not None else (
            branch.fork_depth if branch.fork_depth is not None else tree.NO_DEPTH
        ),
        own_actions=0,
        is_head=(branch.id == adventure.head_branch_id),
        name=branch.name,
        created_at=branch.created_at,
    )


@router.delete("/{adventure_id}/branches/{branch_id}", status_code=204)
def delete_branch(
    adventure_id: int,
    branch_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Throw away a branch, and everything forked from it.

    Nothing auto-prunes a tree, so this is the only thing standing between a
    heavily-retried adventure and unbounded growth — which is why it ships with
    the view that first lets anyone make a fork rather than after it.

    Two branches cannot go. The root, because it holds the turns every other
    branch borrows and deleting it would take the whole story. And the one
    being read — or any branch it was forked from, which is the same mistake
    wearing a disguise: the cascade would take the head out from under the
    player and leave `head_branch_id` pointing at nothing. Switch first.

    The nodes and memories go with it through `ON DELETE CASCADE`, and the
    descendants through `branches.parent_branch_id`'s, so the delete is one
    statement however deep the subtree is.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    branch = get_branch_or_404(adventure, branch_id, db)
    if branch.parent_branch_id is None:
        raise HTTPException(
            400, "This is the story's first branch — deleting it would delete "
                 "the adventure. Delete the adventure itself instead.",
        )
    head = db.get(models.Branch, adventure.head_branch_id)
    # The head's lineage names itself and every branch it borrows from, so one
    # membership test covers both "you are standing on it" and "you are on
    # something forked from it".
    if head is not None and branch.id in {
        entry_id for entry_id, _ in lineage.entries_of(head)
    }:
        raise HTTPException(
            400, "You are reading this branch, or one forked from it. Switch to "
                 "another branch first.",
        )
    acquire_turn_lock(adventure_id)
    try:
        # Collected before the delete, because afterwards there is nothing left
        # to ask which branches went. A cursor left pointing at a deleted branch
        # would be harmless on Postgres, where ids are never reused, and a real
        # bug on SQLite, where the next fork can be handed the id that just went
        # free — at which point a stale anchor silently resolves onto a branch
        # it has never seen.
        doomed = _branch_subtree(db, adventure, branch)
        for cursor in cursors.ALL:
            stored_branch, _ = cursor.stored(adventure)
            if stored_branch in doomed:
                cursor.clear(adventure)
        db.delete(branch)
        adventure.updated_at = models.utcnow()
        db.commit()
    finally:
        _active_turns.discard(adventure_id)
    # The deleted branch's memories go with it, and their cached vectors fall
    # out of the catalogue on the next read — no invalidation call needed. See
    # the note on memorybank's cache.


def _branch_subtree(
    db: Session, adventure: models.Adventure, root: models.Branch
) -> set[int]:
    """`root` and every branch descended from it, by parent pointer.

    Walked over the adventure's own branch rows rather than queried per level:
    an adventure has a handful of branches, and the walk is the same cost as
    one round trip while a recursive CTE would have to be written twice for the
    two dialects this codebase keeps parity with.
    """
    children: dict[int | None, list[int]] = {}
    for bid, parent in db.query(models.Branch.id, models.Branch.parent_branch_id).filter(
        models.Branch.adventure_id == adventure.id
    ):
        children.setdefault(parent, []).append(bid)
    found: set[int] = set()
    stack = [root.id]
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(children.get(current, ()))
    return found


@router.post(
    "/{adventure_id}/branches/{branch_id}/switch", response_model=schemas.ActionPage
)
def switch_branch(
    adventure_id: int,
    branch_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Read and play a different branch of the story.

    Nothing is copied and nothing is rewritten — the head pointer moves, and
    the shared script/world state comes back to what that branch's tip left
    behind. That last part is why a switch is safe at all: the scoreboard and
    the RPG layer are per-adventure, so a branch that did not restore them
    would be told a story with another branch's numbers under it (the
    world-state cooldown clock included, which lives inside the snapshot).
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    branch = db.get(models.Branch, branch_id)
    if branch is None or branch.adventure_id != adventure.id:
        raise HTTPException(404, "Branch not found")
    acquire_turn_lock(adventure_id)
    try:
        adventure.head_branch_id = branch.id
        tree.refresh_head(db, adventure)
        attempts.restore_state(adventure, db_tip(db, adventure))
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(adventure)
        return current_window(db, adventure)
    finally:
        _active_turns.discard(adventure_id)


def db_tip(db: Session, adventure: models.Adventure) -> models.Action | None:
    """The newest node of the story as it now stands, with its outcome loaded."""
    return (
        db.query(models.Action)
        .filter(
            models.Action.adventure_id == adventure.id,
            lineage.path_of(db, adventure).clause(models.Action),
        )
        .options(
            undefer(models.Action.state_after),
            undefer(models.Action.world_state_after),
        )
        .order_by(models.Action.depth.desc(), models.Action.id.desc())
        .first()
    )


@router.post(
    "/{adventure_id}/actions/{action_id}/fork", response_model=schemas.ActionPage
)
def fork_from_attempt(
    adventure_id: int,
    action_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Take the story down this attempt, forking a branch if it has to.

    Three cases, and the first two are not forks:

    * the attempt is already the one the story tells — nothing to do;
    * its turn is the tip, so the attempts are still leaves nobody has built
      on: switch, exactly as `/variant` does, and no branch is created;
    * the story has moved past its turn: fork. The attempt gets a branch of its
      own and the line it is leaving keeps every turn it has.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    # Asked before the shape of the turn is, because a fork leaves the promoted
    # attempt alone on its branch: a client that repeats the call — a double
    # click, a retried request — must get the same answer, not a complaint that
    # the turn it just forked has nothing to fork to.
    if action.live and action.branch_id == adventure.head_branch_id:
        return current_window(db, adventure)
    if len(attempts.group(db, action)) < 2:
        raise HTTPException(
            400, "This turn has only one take, so there is nothing to fork to."
        )
    acquire_turn_lock(adventure_id)
    try:
        newest = last_action(adventure, db)
        at_the_tip = (
            newest is not None
            and newest.branch_id == action.branch_id
            and newest.depth == action.depth
        )
        if at_the_tip:
            # The story at this coordinate is about to say something else, so
            # what was derived from it is withdrawn — the same move retry
            # makes. A fork needs none of that: it leaves the coordinate, and
            # its memory, exactly where they are (see `tree.fork`).
            memorybank.forget_node(db, adventure, action)
            cursors.rewind_all(adventure, action.branch_id, (action.depth or 0) - 1)
            attempts.make_live(db, adventure, action)
        else:
            tree.fork(db, adventure, action)
            attempts.restore_state(adventure, action)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(adventure)
        return current_window(db, adventure)
    finally:
        _active_turns.discard(adventure_id)


@router.post("/{adventure_id}/undo", response_model=schemas.ActionPage)
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
            .filter(
                models.Action.adventure_id == adventure.id,
                lineage.path_of(db, adventure).clause(models.Action),
            )
            .order_by(models.Action.depth.desc(), models.Action.id.desc())
            .limit(2)
            .all()
        )
        if not newest or newest[0].type == "start":
            raise HTTPException(400, "Nothing to undo")
        last = newest[0]
        before_that = newest[1] if len(newest) > 1 else None
        # Only ground this branch owns. Everything before the fork is borrowed
        # from an ancestor and is *its* story too, so taking back a turn here
        # must never reach across and delete a turn out of another branch. The
        # test is on the row's own branch rather than on the fork depth,
        # because that is the fact that decides it.
        if last.branch_id != adventure.head_branch_id:
            raise HTTPException(
                400, "Nothing to undo on this branch — the turns before it "
                     "belong to the branch it was forked from.",
            )
        first_removed = last
        if (last.type == "ai" and before_that is not None
                and before_that.type in ("do", "say", "story")
                and before_that.branch_id == adventure.head_branch_id):
            first_removed = before_that
        # Where the story stands once the turn is gone: what the node in front
        # of the earliest removed one left behind. Read before the deletes, so
        # the question is asked of a story that still has them in it.
        restore_to = attempts.preceding(db, adventure, first_removed)
        delete_turn(db, adventure, last)
        if first_removed is not last:
            delete_turn(db, adventure, first_removed)
        attempts.restore_state(adventure, restore_to)
        db.flush()  # apply the deletes before anything reads the story back
        db.expire(adventure, ["actions"])
        # The tip moved back with them.
        tree.refresh_head(db, adventure)
        db.commit()
        db.refresh(adventure)
        # The newest window, not the whole story: the client replaces its
        # transcript with this, and the transcript is a window now. Returning
        # everything here would undo the paging on the one action most likely
        # to be repeated several times in a row.
        actions, total, has_more = action_window(db, adventure)
        return schemas.ActionPage(
            actions=[schemas.ActionOut.model_validate(a) for a in actions],
            total=total,
            has_more=has_more,
        )
    finally:
        _active_turns.discard(adventure_id)


# ---------- Import / Export ----------

@router.get("/{adventure_id}/export")
def export_adventure(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Full backup: plot components, story cards, scripts (+state), the tree.

    The format is `app/bundle.py`'s alone — both versions of it. A backup is the
    one thing here that outlives the schema, so nothing about its shape is
    decided at a call site.
    """
    adv = get_adventure_or_404(adventure_id, db, user)
    return bundle.export(db, adv)


@router.post("/import", response_model=schemas.AdventureOut, status_code=201)
def import_adventure(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    version = bundle.check_format(payload)
    limits.rate_limit("import", request, user)
    limits.check_row_cap("adventures", db, user)
    limits.check_bundle_lists(
        story_cards=payload.get("storyCards"),
        memories=payload.get("memories"),
        actions=payload.get("actions"),
        branches=payload.get("branches"),
    )
    # The tree is checked before the adventure row exists, so a file that
    # disagrees with itself is a 400 and not a half-imported adventure holding a
    # story with a hole in it.
    story = bundle.plan(payload, version)

    # Raw-dict import bypasses the schemas — clamp strings headed for VARCHAR
    # columns (Postgres enforces the widths; see schemas.py).
    adventure = models.Adventure(
        user_id=user.id,
        title=str(payload.get("title") or "Imported Adventure")[:schemas.NAME_MAX],
        memory=str(payload.get("memory") or ""),
        authors_note=str(payload.get("authorsNote") or ""),
        ai_instructions=str(payload.get("aiInstructions") or ""),
        story_summary=str(payload.get("storySummary") or ""),
        script_state=payload.get("scriptState") or {},
        world_state=payload.get("worldState") or {},
        auto_summarize=bool(payload.get("autoSummarize", False)),
        memory_bank_enabled=bool(payload.get("memoryBankEnabled", False)),
    )
    db.add(adventure)
    db.flush()

    for card in payload.get("storyCards") or []:
        if isinstance(card, dict):
            db.add(models.StoryCard(
                adventure_id=adventure.id,
                type=str(card.get("type") or "")[:schemas.CARD_TYPE_MAX],
                name=str(card.get("name") or "")[:schemas.NAME_MAX],
                keys=str(card.get("keys") or ""),
                entry=str(card.get("entry") or ""),
                notes=str(card.get("notes") or ""),
            ))

    for i, s in enumerate(payload.get("scripts") or []):
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

    bundle.write(db, adventure, story)

    # The anchors and the legacy counts are the same boundary in two coordinate
    # systems, and lining them up needs the actions queryable — this is the only
    # moment both exist.
    db.flush()
    db.expire(adventure, ["actions"])
    bundle.settle(db, adventure, story)

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
    get_adventure_or_404(adventure_id, db, user)
    # A query naming its columns, not a walk of `adventure.memories`. The walk
    # is what retrieval used to do, and it is the reason a turn cost megabytes:
    # a relationship load takes whole entities, so it picks up whatever the
    # model happens to carry. `embedding_blob` is deferred and so would stay
    # out today — this is about the next wide column, not that one.
    #
    # Adventure-wide, not path-scoped, and that is the split: retrieval reads
    # the story being played, the drawer manages the bank. Hiding a branch's
    # memories from the drawer would mean memories nobody can find to delete,
    # in a phase whose rule is that nothing is ever removed automatically.
    return (
        db.query(models.Memory)
        .options(load_only(*MEMORY_LIST_COLUMNS))
        .filter(models.Memory.adventure_id == adventure_id)
        .order_by(models.Memory.id)
        .all()
    )


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
    # No node produced this one, so it gets a branch but no depth.
    tree.place_memory(db, adventure, memory)
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

@router.get("/{adventure_id}/actions", response_model=schemas.ActionPage)
def list_actions(
    adventure_id: int,
    before_id: int | None = None,
    limit: int = ACTION_PAGE,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """A page of the story, walking backwards from the newest action.

    `before_id` is the oldest action the caller already holds, so scrolling up
    is "give me what comes before this". Omit it for the newest window. See
    action_window for why this anchors on a row rather than an offset.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    limit = max(1, min(limit, ACTION_PAGE * 4))
    actions, total, has_more = action_window(
        db, adventure, before_id=before_id, limit=limit
    )
    return schemas.ActionPage(
        actions=[schemas.ActionOut.model_validate(a) for a in actions],
        total=total,
        has_more=has_more,
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
    # One row, one text. Nothing mirrors it any more, so nothing has to be kept
    # in step — the edit used to have to be written into the live variant entry
    # as well, or paging away and back silently reverted it.
    action.text = payload.text
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
    # Same as undo: the turn goes, attempts and all, and whatever it produced
    # is withdrawn. Nothing else needs doing — the marks are depths, and a
    # depth does not move because an action in front of it went away.
    delete_turn(db, adventure, action)
    db.flush()
    db.expire(adventure, ["actions"])
    # Deleting the newest action moves the tip; deleting a middle one leaves a
    # gap in the depths, deliberately — see _backfill_tree.
    tree.refresh_head(db, adventure)
    db.commit()
