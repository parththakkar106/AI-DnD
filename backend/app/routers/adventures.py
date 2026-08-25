import json
import re
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, load_only, undefer
from sqlalchemy.orm.attributes import set_committed_value

from .. import (
    analytics, attempts, auth, bundle, images, limits, memorybank, models, schemas,
    tree, worldstate,
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

# The columns `schemas.ActionOut` renders, listed explicitly.
#
# `deferred=True` in `models.py` keeps the four heavy columns out of bulk reads,
# but each new column then has to opt in to staying narrow. Both egress
# regressions this project has had came from a column that did not opt in. This
# tuple inverts the default: a new column costs nothing until you add it here.
#
# `world_delta` is listed because `ActionOut.world_changes` is computed from it.
# Omitting it saves no bytes. It converts one bulk read into one lazy load per
# row.
ACTION_LIST_COLUMNS = (
    models.Action.adventure_id,
    models.Action.index,
    models.Action.type,
    models.Action.text,
    models.Action.reasoning,
    models.Action.world_delta,
    models.Action.variant_count,
    models.Action.variant_index,
    # SP9: the pager's key. If `parent_id` were deferred, every row on the page
    # would cost a lazy load, which is the cost `load_only` is here to prevent.
    # `branch_id` is listed for the same reason. The pager reads it to tell a
    # local step from a branch switch.
    models.Action.parent_id,
    models.Action.branch_id,
    models.Action.created_at,
)

# How many actions an adventure opens with, and how many arrive per scroll.
#
# Opening a finished adventure once fetched the whole story in one response.
# That reached 589.5 kB for the longest story in production, and it grew as
# stories grew. 60 actions is a few screens of reading. The common case of
# opening a story, reading the end, and taking a turn never pages, and the worst
# case is bounded by the window size rather than by the length of the story.
ACTION_PAGE = 60


def action_window(
    db: Session,
    adventure: models.Adventure,
    before_id: int | None = None,
    limit: int = ACTION_PAGE,
) -> tuple[list[models.Action], int, bool]:
    """Returns the `limit` actions immediately older than `before_id`, oldest first.

    The return value is `(actions, total, has_more)`. If `before_id` is `None`,
    the newest window is returned.

    The query is scoped to the head branch's lineage, which is the story being
    played, rather than to the adventure. A sibling branch's turns therefore
    never appear in the transcript. `total` counts the same path, because it is
    what tells the reader that more actions exist above.

    The window is anchored on an action, never on a count or on arithmetic over
    depth, for two reasons:

    * Appends. Counting back from the newest action shifts every older position
      when a turn lands. A reader who scrolls up while a turn is generating gets
      a window that is one row off, which re-sends one action and skips another.
      An anchor is stable, because "older than this action" means the same thing
      before and after the story grows.
    * The story tree. Depth is dense today, and branching ends that. Comparing
      depths to order a path still works, but treating them as positions does
      not.

    `has_more` comes from requesting one row past the window rather than from a
    second count, so it costs one row instead of a scan.
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
            # The anchor was deleted while the reader scrolled, by an undo or
            # by an edited turn, or it belongs to a branch this story is not on.
            # No row can be older than a row that is not present, so report the
            # end of the story rather than guess and return a duplicate page.
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


# The columns `schemas.MemoryOut` renders. `embedded` is a real column and
# belongs here. The vector it describes does not.
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


# How many characters of the last narration a Continue card shows. The limit is
# long enough to re-establish the scene and short enough to keep the card
# small.
SNIPPET_MAX = 220


def _snippet(text: str) -> str:
    """Condenses stored action text into a single line for a card."""
    # The streaming handler below strips the world-state block before storing AI
    # text, so this function only has to normalize whitespace.
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= SNIPPET_MAX:
        return collapsed
    # Cut at a word boundary rather than mid-word. CSS adds the ellipsis.
    cut = collapsed[:SNIPPET_MAX].rsplit(" ", 1)[0]
    return f"{cut}…"


# Action types that read as narration. `start` is the scenario's opening prompt,
# which is the only text a newly created adventure has. Without `start`, a new
# story's card would show no text at all. `do` and `say` are excluded because
# the card quotes the story's voice rather than the player's.
NARRATION_TYPES = ("ai", "story", "start")


def _latest_narration(db: Session, head_branches: dict[int, int | None]) -> dict[int, str]:
    """Maps each adventure id to the text of its most recent narrated action.

    This runs one window-function query rather than one lookup per adventure, so
    the list endpoint makes a fixed number of round trips.

    The query is scoped by head branch rather than by the full lineage, and this
    is the only place in the codebase that does so. A lineage clause per
    adventure would add a hundred OR terms to the index screen's query to select
    one row each. The two scopes differ only for a branch with no nodes of its
    own, and playing a turn onto a branch is what creates it, so that state does
    not occur. An adventure with no branch has no story to quote.
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
            # Sibling attempts share a depth, and the newest has the highest
            # id. Without this filter the snippet quotes the attempt written
            # last rather than the one the story tells. After you switch back to
            # an earlier attempt, the index screen would quote the discarded one
            # and disagree with the story on screen.
            models.Action.live.is_(True),
        )
        .subquery()
    )
    rows = db.query(ranked.c.adventure_id, ranked.c.text).filter(ranked.c.rank == 1).all()
    return {adventure_id: text for adventure_id, text in rows}


@router.get("", response_model=list[schemas.AdventureListItem])
def list_adventures(db: Session = Depends(get_db), user: models.User = CurrentUser):
    # Select named columns rather than the whole Adventure entity. The entity is
    # sixteen columns wide and includes `script_state`, `world_state`,
    # `placeholders`, `story_summary`, `memory`, `authors_note`, and
    # `ai_instructions`. That is about 15 kB per row in production, fetched once
    # per adventure on every index load, and this screen uses none of it. Naming
    # the columns also means a wide column added to Adventure later has to opt
    # in to being listed here.
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
        # Group by both primary keys. Postgres requires every selected column
        # to be grouped or aggregated. The Adventure columns are covered by its
        # own grouped primary key, but the Scenario columns come from a joined
        # table and have to be listed as well. SQLite accepts the shorter form,
        # and Postgres rejects it.
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
            # The art belongs to the scenario, so the cache-busting stamp uses
            # the scenario's `updated_at`, not the adventure's.
            image_url=images.public_url(scenario_id, image or "", scenario_updated),
            icon=icon or "",
        )
        # `count` counts every action in the adventure, not only the ones on
        # the path. With one branch the two numbers are equal. After forking
        # ships, the index screen overstates a story that has sibling branches.
        # The fix belongs to SP5, which is where a fork can first exist.
        for (adv_id, scenario_id, title, updated_at, _head_branch_id, count,
             scenario_title, image, icon, scenario_updated) in rows
    ]


PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    """Replaces `${Name}` with the player-provided value.

    Unknown names are left unchanged.
    """
    if not text or not values:
        return text
    return PLACEHOLDER_RE.sub(
        lambda m: values.get(m.group(1).strip(), m.group(0)), text
    )


# Adventure fields that start as a copy of the scenario's text, so "Update from
# scenario" can copy them again. `title` is excluded because it is the
# adventure's own name, which players rename. `story_summary` is excluded
# because it is play output rather than scenario content.
SCENARIO_TEXT_FIELDS = ("memory", "authors_note", "ai_instructions")

# Story-card fields that are copied from the scenario and compared to detect
# drift.
CARD_FIELDS = ("type", "name", "keys", "entry", "notes")


def scenario_card_specs(scenario: models.Scenario, values: dict[str, str]) -> dict[str, dict]:
    """Returns every story card a scenario implies, keyed by a stable `source_ref`.

    The result holds the scenario's own cards, keyed `card:<id>`, plus one card
    per NPC defined in its `stat_schema`, keyed `npc:<key>`. Placeholders are
    already filled in.

    Adventure creation and refresh both call this function, so the two cannot
    diverge.
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
    # Phase 12: each defined NPC gets a story card, so its description works as
    # lore and can trigger in a scene. If a card with that name already exists,
    # skip the NPC.
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
        # A scenario is playable if the user owns it or if it is public.
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
        # Stored so that a later "Update from scenario" fills the copied text
        # with the same answers instead of inserting literal `${...}` tokens.
        placeholders=dict(values),
    )
    db.add(adventure)
    db.flush()
    # Give every adventure a story tree as soon as it exists, before anything
    # is played onto it. Otherwise the tree code has to tolerate a NULL head
    # everywhere, which buys nothing.
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
            # Record the starting state on the opening node, so undoing or
            # retrying the first turn has a state to roll back to.
            attempts.snapshot_outcome(adventure, opening)
            tree.place_action(db, adventure, opening)
            db.add(opening)

    db.commit()
    db.refresh(adventure)
    analytics.record_event(analytics.EV_ADVENTURE, user)
    # Track which shared scenarios players pick. This is the only content this
    # module records, and it records only public scenarios. A player's own
    # scenario titles stay private.
    if scenario is not None and scenario.is_public:
        analytics.record(analytics.M_SCENARIO, scenario.title)
    return adventure


@router.get("/{adventure_id}", response_model=schemas.AdventureOut)
def get_adventure(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Returns the adventure and the newest window of its story.

    `actions` holds the last `ACTION_PAGE` actions, not all of them.
    `action_count` reports the real total, so the reader knows that more actions
    exist above. `GET /{id}/actions` serves the older pages as the reader
    scrolls up.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    actions, total, _ = action_window(db, adventure)
    # Annotate before handing over the window. This path serializes through the
    # relationship rather than building `ActionOut` itself, so the pager numbers
    # have to be on the rows before Pydantic reads them.
    annotate_takes(db, adventure.id, actions)
    # Attach the window as if the relationship had loaded it.
    # `set_committed_value` is the only safe way to do this. Assigning
    # `adventure.actions = [...]` marks the collection dirty, and the
    # relationship cascades delete-orphan, so the next flush deletes every
    # action outside the window. `set_committed_value` records the rows as the
    # already-loaded, unmodified value, so serialization triggers no lazy load
    # and leaves nothing pending.
    set_committed_value(adventure, "actions", actions)
    out = schemas.AdventureOut.model_validate(adventure)
    out.action_count = total
    return out


@router.get("/{adventure_id}/script-state")
def get_script_state(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Returns the scripting `state` object.

    The object holds every variable that scripts read and write through
    `state.x`, persisted after each hook. It stays `{}` until a script sets a
    variable.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    return {"state": state}


@router.get("/{adventure_id}/world-state")
def get_world_state(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Returns the live RPG world state and the scenario's `stat_schema`.

    The play view uses both to render the character sheet and the milestones.
    `schema` is null when the adventure has no RPG layer.
    """
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
    """Edits the live RPG values directly, as a manual correction rather than a turn.

    `overrides` maps paths such as `player.hp`, `npc.gwen.trust`, `flags.x`, and
    `milestones.y` to their new absolute values. The endpoint rejects unknown
    paths and wrong types one at a time, and applies the rest.
    """
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
# Retry first deleted the AI action and generated a replacement. A later version
# kept the row and appended each attempt to a JSON list on it. Now every attempt
# is its own node on the same branch at the same depth, and exactly one of them
# is `live`. `app/attempts.py` owns the group and its invariants. The endpoints
# below only query it.


def world_delta_of(snapshot: dict | None) -> dict | None:
    """Returns the bulk-read slice of a context snapshot, for `Action.world_delta`.

    `context_snapshot` is deferred because it holds the whole assembled prompt.
    The two parts that every action needs, the world-change chips and the emit
    block replayed into history, get their own small column instead. Update this
    function wherever a snapshot is written.
    """
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
    # No later request reads this adventure's vectors, so drop them now. The
    # cache would otherwise hold them until the process restarted.
    memorybank.forget_cached_vectors(adventure_id)


# ---------- Turn engine ----------

# One turn at a time per adventure. The set lives in memory, which is enough for
# a single-process local app. Sync endpoints run in a threadpool, so the
# check-and-add needs a lock. The check also has to run during the request
# rather than when the SSE generator first runs. Otherwise two rapid requests
# both pass the check and generate concurrently.
_active_turns: set[int] = set()
_active_turns_guard = threading.Lock()


def acquire_turn_lock(adventure_id: int):
    """Claims the adventure's turn slot atomically. `with_turn_lock` releases it."""
    with _active_turns_guard:
        if adventure_id in _active_turns:
            raise HTTPException(409, "A turn is already generating for this adventure.")
        _active_turns.add(adventure_id)


async def with_turn_lock(adventure_id: int, gen):
    """Wraps an SSE generator so that it releases the `acquire_turn_lock` lock."""
    try:
        async for event in gen:
            yield event
    finally:
        _active_turns.discard(adventure_id)


def format_player_input(action_type: str, text: str) -> str:
    """Formats player input the way AI Dungeon does."""
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
    return text  # The "story" type is appended as raw text.


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def turn_error(detail: str, **extra) -> str:
    """Returns an SSE error for a turn that could not be produced, and counts it.

    A failed turn is still an HTTP 200 response, so the middleware's status-code
    tally cannot see it. This metric exists so that a demo whose model refuses
    every request does not report as healthy.
    """
    analytics.record(analytics.M_EVENT, analytics.EV_TURN_ERROR)
    return sse({"type": "error", "detail": detail, **extra})


# `no-cache` stops an intermediary from caching the stream. `X-Accel-Buffering`
# makes nginx-style reverse proxies, which hosted deploys use, flush each event
# immediately rather than buffer it.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def action_json(action: models.Action, db: Session | None = None) -> dict:
    """Serializes one action for the wire.

    Passing `db` fills in the pager numbers, and a turn that was just played must
    pass it. The attempt that turn created is often the second one at its
    coordinate, so the message needs a pager that the client cannot infer from a
    count of one. Without `db`, a retry showed no pager until the page reloaded.
    The adventure GET has the same requirement and builds its window a third way.
    """
    if db is not None:
        annotate_takes(db, action.adventure_id, [action])
    return schemas.ActionOut.model_validate(action).model_dump(mode="json")


def next_index(adventure: models.Adventure) -> int:
    return context_history.max_action_index(adventure) + 1


def next_depth(adventure: models.Adventure) -> int:
    """Returns the depth for the next node played onto this story, one past the tip.

    This is not `next_index`, which returned the same number until SP5. `index`
    has to stay unique across the whole adventure, because it is the v1 bundle's
    key. On a story forked at depth 6 after twenty turns, `next_index` gives the
    next node depth 21 and leaves a fourteen-deep gap in the path. A depth is a
    position along this one story, and the branch is what makes it unambiguous.
    """
    return adventure.head_depth + 1


def last_action(adventure: models.Adventure, db: Session) -> models.Action | None:
    """Returns the newest action of any kind on the story being played, or `None`.

    This runs a query rather than reading `adventure.actions[-1]`, which loads
    the entire story to read one row. That collection also holds every branch's
    actions, so it sometimes returns a row from the wrong branch.
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
    """Streams the AI continuation as SSE, then stores the result.

    The continuation passes through the `context` and `output` script hooks.

    If `retry_of` is set, the result is stored as a sibling of that AI action, at
    the same turn and the same coordinate, and the discarded attempt stays where
    it was written. Before calling, the caller must roll the adventure back to
    the state before that turn. See `retry_action`. If this generator ends
    without saving, the rollback is undone, so the state cannot diverge from the
    text on screen.
    """
    saved = False
    try:
        async for event in _generate_turn(adventure, db, pipeline, user, retry_of):
            if event is _SAVED:
                saved = True
                continue
            yield event
    finally:
        if retry_of is not None and not saved:
            # The turn failed with a provider error, an empty reply, a script
            # stop, or a disconnected client. No sibling was written, so the
            # attempt on screen is still the live one. Restore the state it
            # produced.
            attempts.restore_state(adventure, retry_of)
            db.commit()


# `_generate_turn` yields this sentinel once the action is committed. It tells
# the wrapper above to leave the rollback in place rather than reverse it.
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
    # On a retry, the attempt being replaced is still the live node of its turn,
    # because it stays live until a replacement exists. Filter it out of the
    # context. Otherwise the model reads the attempt it is replacing as
    # established story and writes a sequel to it.
    replacing_id = retry_of.id if retry_of is not None else None
    if cfg.using_demo:
        # The server-funded key makes no embedding or summarization calls, so
        # memory retrieval is skipped. If the bank is on, return a note.
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

    # onModelContext: scripts read, and can rewrite, the whole assembled
    # context.
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
        yield turn_error(str(exc))
        return

    text = "".join(chunks).strip()
    # The model's literal reply, kept for the Insights "Raw AI output" view. It
    # still contains the world-state block, which the code below strips.
    raw_output = text
    if not text:
        # The model streamed reasoning but no story text, so it spent its whole
        # budget on reasoning. Report that rather than "empty response".
        if reasoning_chunks:
            detail = (
                "The model used its entire token budget on reasoning and returned no "
                'story text. Raise "Max output tokens" in Settings, set a "Reasoning '
                'max tokens" cap, or switch to a non-reasoning model.'
            )
        else:
            detail = "The AI returned an empty response."
        yield turn_error(detail)
        return

    # onOutput
    text, _ = pipeline.run("output", text)
    if not text.strip():
        yield turn_error("A script's output modifier returned empty text.")
        return
    snapshot["script"] = snapshot["script"] | pipeline.report()

    # RPG world state (Phase 12): read the AI's state delta out of the reply,
    # apply it through the engine, and strip the block from the displayed text.
    #
    # A retry re-runs the same turn, so it is played at that turn's depth. The
    # cooldown rules run on a position in the story, and a second attempt at turn
    # 12 is still turn 12. This was `retry_of.index`, which held the same number
    # until SP4. Depth stays correct once a branch has its own numbering.
    ai_depth = retry_of.depth if retry_of is not None else next_depth(adventure)
    stat_schema = adventure.scenario.stat_schema if adventure.scenario else None
    if worldstate.has_schema(stat_schema):
        text, delta = worldstate.extract_delta(text)
        if not text.strip():
            yield turn_error("The AI returned only a state update and no story text.")
            return
        new_world_state, ws_report = worldstate.apply_delta(
            adventure.world_state, stat_schema, delta, ai_depth
        )
        adventure.world_state = new_world_state
        snapshot["world_state"] = {"delta": delta, "report": ws_report, "state": new_world_state}

    snapshot["raw_output"] = raw_output
    # The cost the endpoint reports for the call, including how much of the
    # prompt came from cache rather than being billed in full. This is recorded
    # per attempt, next to the prompt it priced.
    snapshot["usage"] = provider.last_usage

    reasoning = "".join(reasoning_chunks).strip() or None
    ai_action = models.Action(
        adventure_id=adventure.id,
        # A sibling shares the turn's legacy index for the same reason it
        # shares its depth: it is the same turn. Two rows then hold one index,
        # which is safe, because `max_action_index` takes a maximum rather than
        # a count, and nothing else reads the column.
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
        # The text at this coordinate changed, so anything derived from it no
        # longer describes the story. Withdraw the memory attached to the node
        # and return that stretch to both passes. Before SP4 this code was
        # unreachable, because the summarizer held the newest action back until
        # a turn landed on top of it. See `memorybank`.
        memorybank.forget_node(db, adventure, retry_of)
        cursors.rewind_all(adventure, retry_of.branch_id, ai_depth - 1)
        db.flush()
        attempts.renumber(attempts.group(db, ai_action))
    else:
        tree.place_action(db, adventure, ai_action)
        db.add(ai_action)
    adventure.updated_at = models.utcnow()
    if cfg.using_demo:
        # Successful demo turns count against the daily cap, which the endpoint
        # checks before the turn starts. A failed provider call above returns
        # before this line.
        auth.count_demo_turn(user)
    db.commit()
    # Count the turn here, after every path on which it could still have failed,
    # so the number means "stories advanced" rather than "requests attempted".
    # The demo tally counts those same turns as spend on the server-funded key.
    analytics.record_event(analytics.EV_TURN, user)
    if cfg.using_demo:
        analytics.record(analytics.M_EVENT, analytics.EV_DEMO_TURN)
    db.refresh(ai_action)
    yield _SAVED
    yield sse({"type": "done", "action": action_json(ai_action, db), "script": pipeline.report()})
    # Phase 6: schedule summarization and embedding without waiting for them.
    # The task opens its own database session. It is skipped on the demo key,
    # because background AI calls are unmetered spend on the server-funded
    # key.
    if not cfg.using_demo:
        memorybank.schedule_post_turn(adventure)


def check_demo_cap(db: Session, user: models.User) -> None:
    """Checks the demo cap before a turn starts.

    Checking first avoids storing a capped player's input and then leaving it
    without a reply.
    """
    settings = get_settings(db, user)
    if auth.resolve_provider_config(settings).using_demo and auth.demo_turns_left(user) <= 0:
        raise HTTPException(429, auth.DEMO_CAP_MESSAGE)


async def run_player_turn(
    adventure: models.Adventure,
    db: Session,
    payload: schemas.ActionCreate,
    user: models.User,
    preformatted: bool = False,
):
    """Plays a player's turn: their action, then the reply to it.

    `preformatted` means the text already carries the `> You ...` conventions and
    is written as-is. That applies when the player retakes a turn they already
    played (SP9). The editor is seeded with the stored text, which is already
    formatted, and a plain edit puts that same text in the box and writes it back
    verbatim. Formatting it a second time produces `> You > You ...`.
    """
    pipeline = ScriptPipeline(adventure, db)

    # An empty do, say, or story action behaves as a continue.
    if payload.type != "continue" and payload.text.strip():
        # onInput reads the formatted text, as in AI Dungeon: "> You ...".
        formatted = (
            payload.text.strip() if preformatted
            else format_player_input(payload.type, payload.text)
        )
        modified, stop = pipeline.run("input", formatted)
        if not modified.strip():
            yield turn_error("A script's input modifier returned empty text.",
                             script=pipeline.report())
            return
        player_action = models.Action(
            adventure_id=adventure.id,
            index=next_index(adventure),
            depth=next_depth(adventure),
            type=payload.type,
            text=modified,
        )
        # The state after the input hook has run. The node leaves this state
        # behind. The AI turn after it starts here, and a retry of that turn
        # rolls back to here.
        attempts.snapshot_outcome(adventure, player_action)
        tree.place_action(db, adventure, player_action)
        db.add(player_action)
        db.commit()
        db.refresh(player_action)
        # The new action was added through its foreign key, so the loaded
        # `adventure.actions` collection is stale. Without this expire,
        # `build_context` and `next_index` for the AI action do not see the
        # player action that was just saved.
        db.expire(adventure, ["actions"])
        yield sse({"type": "player", "action": action_json(player_action, db)})
        if stop:
            # If onInput returns `{ stop: true }`, skip the AI call.
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
    try:
        _move_to_after(db, adventure, payload.after_id)
    except BaseException:
        _active_turns.discard(adventure_id)
        raise
    return StreamingResponse(
        with_turn_lock(adventure_id, run_player_turn(adventure, db, payload, user)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _move_to_after(
    db: Session, adventure: models.Adventure, after_id: int | None
) -> None:
    """Moves the story to `after_id` before the turn is played.

    This is where a branch is created (SP9). Reading an attempt that the story
    moved past changes nothing on the server. Writing below one is the first time
    the player states which line they mean, and that is when the fork happens.

    An attempt already on the path needs no move, because the story is already
    there.
    """
    if after_id is None:
        return
    node = db.get(models.Action, after_id)
    if node is None or node.adventure_id != adventure.id:
        raise HTTPException(404, "Action not found")
    if node.live and lineage.path_of(db, adventure).contains(node):
        return
    if not node.live and len(attempts.group(db, node)) < 2:
        # The pager cannot reach this node, so no legitimate action put the
        # player here.
        raise HTTPException(400, "That take is not one of this turn's.")
    stand_on(db, adventure, node)
    db.commit()
    db.refresh(adventure)


@router.post("/{adventure_id}/retry")
def retry_action(
    adventure_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Regenerates the last AI action and keeps the discarded attempt.

    The attempt on screen stays as it was written. The shared script state and
    world state roll back to what the node before it left behind, and the new
    attempt is stored as a sibling at the same coordinate. No text the AI wrote
    is rewritten or deleted.
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
            # Roll the state back to before this AI turn's hooks ran, so that
            # regenerating starts from a clean state rather than applying output
            # mutations on top of the attempt being replaced. If the preceding
            # node has no snapshot, which happens for a pre-SP4 row that the
            # migration could not derive one for, this call does nothing and
            # leaves the state as it is.
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
    """Returns every attempt made for one AI turn.

    The client fetches these on demand, because the adventure payload carries
    only the counts. That keeps old narration out of every page load.

    You can address the turn by any of its attempts, not only the live one.
    Switching changes which row the story tells, and a client that holds an id it
    received a moment ago still has to be able to ask about the same turn.
    """
    get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    rows = attempts.group(db, action)
    if len(rows) < 2:
        return []  # Never retried, so the turn has one attempt.
    return [
        schemas.VariantOut(
            id=row.id,
            index=i,
            text=row.text,
            reasoning=row.reasoning,
            branch_id=row.branch_id,
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
    """Makes an earlier attempt live again and restores the state it produced.

    The restored state covers both the script state and the world state.

    Only the last action can be switched. The turns after an older action were
    written to continue the text that is currently active, so replacing that text
    would leave the story contradicting itself. The attempts of earlier turns
    stay readable through `list_variants`.
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
            # The story at this coordinate is about to change, so withdraw
            # anything derived from the previous text. A retry does the same
            # thing for the same reason.
            memorybank.forget_node(db, adventure, chosen)
            cursors.rewind_all(adventure, chosen.branch_id, (chosen.depth or 0) - 1)
        attempts.make_live(db, adventure, chosen)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(chosen)
        # Return the row that is now in the story, which is a different row
        # from the one the request addressed. An attempt is a node, so choosing
        # one moves the story onto it rather than rewriting a row.
        return chosen
    finally:
        _active_turns.discard(adventure_id)


def delete_turn(
    db: Session, adventure: models.Adventure, node: models.Action
) -> None:
    """Removes a turn, including every attempt at it and not only the one on screen.

    A discarded attempt is a leaf at the same coordinate, and the only way to
    reach it is through that coordinate. Leaving it behind when the turn is
    deleted orphans a row that no read can reach. Whatever the turn produced is
    withdrawn once, because a memory is attached to the coordinate rather than to
    one attempt.
    """
    memorybank.forget_node(db, adventure, node)
    # Scoped to this node's branch (SP9). Groups span branches now, and an
    # attempt forked onto its own line belongs to another branch's story. See
    # `attempts.on_branch`.
    for attempt in attempts.on_branch(attempts.group(db, node), node):
        db.delete(attempt)


# ---------- Branches (Phase 14, SP5) ----------
#
# Attempts accumulate at the tip as siblings, which costs nothing. An attempt
# becomes a branch only when the player continues the story from it and leaves
# the line that moved past it. That is the same event as playing a turn past the
# attempt. Creating the branch here rather than on the next turn means a branch
# exists only for a divergence someone built on, and the line being left is not
# modified.


def annotate_takes(
    db: Session, adventure_id: int, actions: list[models.Action]
) -> list[models.Action]:
    """Sets the `2/4` pager numbers on every action on a page (SP9).

    This runs one query for the whole page rather than one per row. The pager
    needs the shape of each turn's attempt group, and calling `attempts.group`
    per action costs one query per message on screen. `variant_count` was cached
    to avoid that cost, which is why SP8 could not drop it.

    This function reads the siblings rather than counting them. A group holds
    only a few attempts, the page is bounded, and a count still needs a second
    query for the ordinal. It fetches only the id and the ordering keys, so it
    stays cheap even when the text is large.
    """
    parents = {a.parent_id for a in actions if a.parent_id is not None}
    if parents:
        rows = (
            db.query(
                models.Action.id,
                models.Action.parent_id,
                models.Action.variant_index,
            )
            .filter(
                models.Action.adventure_id == adventure_id,
                models.Action.parent_id.in_(parents),
            )
            .order_by(models.Action.variant_index, models.Action.id)
            .all()
        )
    else:
        rows = []
    siblings: dict[int, list[int]] = {}
    for row_id, parent_id, _ in rows:
        siblings.setdefault(parent_id, []).append(row_id)
    for action in actions:
        ids = siblings.get(action.parent_id) if action.parent_id else None
        if not ids:
            # A root node, or a pre-SP9 row that the backfill could not place.
            # It has one attempt, which is how it was written.
            action.take_count, action.take_index = 1, 0
            continue
        action.take_count = len(ids)
        action.take_index = ids.index(action.id) if action.id in ids else 0
    return actions


def current_window(db: Session, adventure: models.Adventure) -> schemas.ActionPage:
    actions, total, has_more = action_window(db, adventure)
    return schemas.ActionPage(
        actions=[
            schemas.ActionOut.model_validate(a)
            for a in annotate_takes(db, adventure.id, actions)
        ],
        total=total,
        has_more=has_more,
    )


@router.get("/{adventure_id}/branches", response_model=list[schemas.BranchOut])
def list_branches(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Returns every branch of the adventure and where each one leaves its parent.

    A tree view is drawn from this shape. `fork_depth` gives the depth where the
    line splits off, and `depth` gives the depth where it currently ends. The
    whole picture costs one query over `branches` plus one grouped query over
    `actions`, never one query per branch, so a view of a hundred forks does not
    cost a hundred round trips.
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
            # A branch with no nodes of its own sits at its fork point. That
            # node is the last one its story contains. The node is borrowed, but
            # it is still the tip. This matches `tree.refresh_head`.
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
    """Returns one branch of this adventure.

    If the branch belongs to another adventure, the 404 does not confirm that the
    branch exists.
    """
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
    """Names a branch, or clears the name to leave it unnamed.

    A blank string means the same thing as `null`. A name of only spaces is not a
    name anyone chose, and storing one gives the client an empty label to draw
    instead of the fork depth.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    branch = get_branch_or_404(adventure, branch_id, db)
    name = (payload.name or "").strip()
    branch.name = name or None
    adventure.updated_at = models.utcnow()
    db.commit()
    db.refresh(branch)
    # Read both numbers in one pass, and count them the way `list_branches`
    # counts them, as live rows on this branch. A renamed branch is the same
    # branch, so this response has to match the row the panel would fetch.
    tip, own = (
        db.query(func.max(models.Action.depth), func.count(models.Action.id))
        .filter(
            models.Action.adventure_id == adventure.id,
            models.Action.branch_id == branch.id,
            models.Action.live.is_(True),
        )
        .one()
    )
    return schemas.BranchOut(
        id=branch.id,
        parent_branch_id=branch.parent_branch_id,
        fork_depth=branch.fork_depth,
        depth=tip if tip is not None else (
            branch.fork_depth if branch.fork_depth is not None else tree.NO_DEPTH
        ),
        own_actions=own,
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
    """Deletes a branch and everything forked from it.

    Nothing prunes the tree automatically, so this endpoint is what keeps a
    heavily retried adventure from growing without bound. That is why it ships
    with the view that first lets anyone create a fork rather than after it.

    Two kinds of branch cannot be deleted. The root cannot, because it holds the
    turns every other branch borrows, so deleting it deletes the whole story. The
    branch currently being read cannot, and neither can any branch it was forked
    from, because the cascade would remove the head under the player and leave
    `head_branch_id` dangling. Switch branches first.

    Nodes and memories are deleted by `ON DELETE CASCADE`, and descendants by the
    cascade on `branches.parent_branch_id`, so the delete is a single statement
    however deep the subtree is.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    branch = get_branch_or_404(adventure, branch_id, db)
    if branch.parent_branch_id is None:
        raise HTTPException(
            400, "This is the story's first branch — deleting it would delete "
                 "the adventure. Delete the adventure itself instead.",
        )
    head = db.get(models.Branch, adventure.head_branch_id)
    # The head's lineage lists itself and every branch it borrows from, so one
    # membership test covers both the branch being read and any branch forked
    # from it.
    if head is not None and branch.id in {
        entry_id for entry_id, _ in lineage.entries_of(head)
    }:
        raise HTTPException(
            400, "You are reading this branch, or one forked from it. Switch to "
                 "another branch first.",
        )
    acquire_turn_lock(adventure_id)
    try:
        # Collect the subtree before the delete, because afterwards there is no
        # way to ask which branches were removed. A cursor left pointing at a
        # deleted branch is harmless on Postgres, which never reuses ids, but it
        # is a bug on SQLite, where the next fork can receive the id that was
        # just freed. A stale anchor then resolves onto a branch it never saw.
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
    # The deleted branch's memories are deleted with it, and their cached
    # vectors drop out of the catalogue on the next read, so no invalidation
    # call is needed. See the note on the `memorybank` cache.


def _branch_subtree(
    db: Session, adventure: models.Adventure, root: models.Branch
) -> set[int]:
    """Returns `root` and every branch descended from it, following parent pointers.

    The walk runs over the adventure's own branch rows rather than one query per
    level. An adventure has few branches, so the walk costs one round trip, and a
    recursive CTE would have to be written twice for the two dialects this
    codebase supports.
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
    """Reads and plays a different branch of the story.

    No row is copied and no row is rewritten. The head pointer moves, and the
    shared script state and world state are restored to what that branch's tip
    left behind. The restore is what makes a switch safe. Both states are stored
    per adventure, so a branch that did not restore them would be played with
    another branch's numbers, including the world-state cooldown clock inside the
    snapshot.
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
    """Returns the newest node of the story as it stands, with its outcome loaded."""
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
    """Continues the story from this attempt, forking a branch if one is needed.

    There are three cases, and the first two do not fork:

    * The attempt is already the one the story tells, so there is nothing to do.
    * Its turn is the tip, so the attempts are still leaves that nothing was
      built on. The endpoint switches, as `/variant` does, and creates no branch.
    * The story has moved past its turn, so the endpoint forks. The attempt gets
      a branch of its own, and the line it leaves keeps every turn it has.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    # Check this before checking the shape of the turn, because a fork leaves
    # the promoted attempt alone on its branch. A client that repeats the call,
    # after a double click or a retried request, has to get the same answer
    # rather than an error saying the turn it just forked has nothing to fork
    # to.
    if action.live:
        # A live node already holds what its coordinate says, so there is no
        # attempt here to promote. On the path being read this call does
        # nothing, and it has to stay that way, so that a repeated call after a
        # double click or a retried request gets the same answer. Off the path
        # the node belongs to another line's story, and moving there is a branch
        # switch.
        #
        # The membership test covers the whole lineage, not `head_branch_id`. A
        # head borrows its ancestors' turns, so a live node on an ancestor is
        # already being read. Forking it would move the live row off the parent
        # and promote a sibling in its place, which rewrites the story on a
        # branch nobody asked about and on this one, which borrows that depth.
        if lineage.path_of(db, adventure).contains(action):
            return current_window(db, adventure)
        raise HTTPException(
            400,
            "That take is already the story on another branch. Switch to that "
            "branch to read it.",
        )
    if len(attempts.group(db, action)) < 2:
        raise HTTPException(
            400, "This turn has only one take, so there is nothing to fork to."
        )
    acquire_turn_lock(adventure_id)
    try:
        stand_on(db, adventure, action)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(adventure)
        return current_window(db, adventure)
    finally:
        _active_turns.discard(adventure_id)


@router.post("/{adventure_id}/actions/{action_id}/takes")
def add_take(
    adventure_id: int,
    action_id: int,
    payload: schemas.TakeCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Plays a turn again, whoever wrote it.

    This endpoint replaces two earlier operations. `retry` gave an AI turn
    another attempt, but only for the newest turn, and a player's own message had
    no attempts at all, so changing text you had typed meant overwriting it and
    losing the story it led to. Here an AI turn regenerates, a player turn takes
    the text you supply, and neither depends on where in the story it sits.

    The tip is the only case that needs no branch, and only for an AI turn,
    because nothing was played after it and its attempts are still leaves. A
    player turn is never at the tip, since the reply to it is, so a player turn
    that has been answered always takes a branch.

    A branch is needed here for the same reason `fork` needs one. The turn being
    replayed already has a story after it, and that story was written as a
    continuation of the old text. `branch_at` leaves the path just before this
    turn, so the new attempt is written at the same depth under the same parent,
    and the line it leaves is unchanged. No node below is copied.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.rate_limit("turn", request, user)
    limits.check_row_cap("actions", db, user, adventure=adventure)
    check_demo_cap(db, user)
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure_id:
        raise HTTPException(404, "Action not found")
    if action.type not in ("do", "say", "story", "continue", "ai"):
        # The opening is not a turn anyone played, so it has no second attempt.
        # Editing the scenario is what changes it.
        raise HTTPException(400, "The opening of a story has no other take.")
    if action.depth is None or not lineage.path_of(db, adventure).contains(action):
        raise HTTPException(400, "That turn is not on the story you are reading.")
    acquire_turn_lock(adventure_id)
    retry_of = None
    try:
        newest = last_action(adventure, db)
        at_the_tip = newest is not None and newest.id == action.id
        if at_the_tip and action.type == "ai":
            # Nothing was played after it, so its attempts are still leaves and
            # a branch would serve no purpose. This is the `retry` path.
            retry_of = action
            attempts.roll_back_before(db, adventure, action)
        else:
            # The turn has a story after it, written as a continuation of the
            # text that is there now. The new attempt leaves the path just
            # before the turn, so that story keeps the attempt it was written
            # for.
            tree.branch_at(db, adventure, action.depth - 1)
            attempts.roll_back_before(db, adventure, action)
        adventure.updated_at = models.utcnow()
        db.commit()
        db.refresh(adventure)
    except BaseException:
        _active_turns.discard(adventure_id)
        raise
    if action.type == "ai":
        # There is no player action to write. The action this turn answers is
        # already on the path, borrowed from the line being left.
        stream = generate_turn(
            adventure, db, ScriptPipeline(adventure, db), user, retry_of=retry_of
        )
    else:
        stream = run_player_turn(
            adventure,
            db,
            schemas.ActionCreate(type=action.type, text=payload.text),
            user,
            # The client seeded its editor from the stored text, which already
            # carries the "> You ..." conventions.
            preformatted=True,
        )
    return StreamingResponse(
        with_turn_lock(adventure_id, stream),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def stand_on(
    db: Session, adventure: models.Adventure, action: models.Action
) -> None:
    """Makes `action` the attempt the story tells, forking only if that is needed.

    There are two cases, and the caller does not have to know which one applies.
    While the turn is still the tip, its attempts are leaves that nothing was
    built on, so this is a switch and no branch is created. Once the story has
    moved past the turn, the line being left keeps every turn it has, so the
    attempt needs a branch of its own.

    The fork endpoint calls this function, and so does a turn played below an
    attempt the story moved past. Both are the same operation, once as a request
    and once as a step on the way to writing (SP9).
    """
    newest = last_action(adventure, db)
    at_the_tip = (
        newest is not None
        and newest.branch_id == action.branch_id
        and newest.depth == action.depth
    )
    if at_the_tip:
        # The story at this coordinate is about to change, so withdraw whatever
        # was derived from it. A retry does the same thing. A fork needs none of
        # this, because it leaves the coordinate and its memory where they are.
        # See `tree.fork`.
        memorybank.forget_node(db, adventure, action)
        cursors.rewind_all(adventure, action.branch_id, (action.depth or 0) - 1)
        attempts.make_live(db, adventure, action)
    else:
        tree.fork(db, adventure, action)
        attempts.restore_state(adventure, action)


@router.post("/{adventure_id}/undo", response_model=schemas.ActionPage)
def undo_turn(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    """Deletes the last turn: the trailing AI action and its player action, if any.

    The endpoint also rolls the shared `script_state` back to before that turn
    ran, and it prunes any memory that summarized the removed actions. The turn
    lock prevents an undo while a turn is still generating.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    acquire_turn_lock(adventure_id)
    try:
        # Only the last turn is removed, so fetch the two actions it can
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
        # Undo only what this branch owns. Everything before the fork is
        # borrowed from an ancestor and is part of that ancestor's story too, so
        # an undo here must never delete a turn out of another branch. The test
        # reads the row's own branch rather than the fork depth, because the
        # branch is what decides the case.
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
        # The state the story returns to once the turn is gone, which is what
        # the node before the earliest removed one left behind. Read it before
        # the deletes, while those rows are still in the story.
        restore_to = attempts.preceding(db, adventure, first_removed)
        delete_turn(db, adventure, last)
        if first_removed is not last:
            delete_turn(db, adventure, first_removed)
        attempts.restore_state(adventure, restore_to)
        db.flush()  # Apply the deletes before anything reads the story back.
        db.expire(adventure, ["actions"])
        # The tip moves back with the deleted rows.
        tree.refresh_head(db, adventure)
        db.commit()
        db.refresh(adventure)
        # Return the newest window rather than the whole story. The client
        # replaces its transcript with this response, and the transcript is a
        # window. Returning everything would defeat the paging on the action a
        # player is most likely to repeat several times in a row.
        actions, total, has_more = action_window(db, adventure)
        return schemas.ActionPage(
            actions=[
            schemas.ActionOut.model_validate(a)
            for a in annotate_takes(db, adventure.id, actions)
        ],
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
    """Returns a full backup: plot components, story cards, scripts, state, and tree.

    `app/bundle.py` owns the format, in both of its versions. A backup outlives
    the schema, so no call site decides anything about its shape.
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
    # Check the tree before the adventure row exists, so that an inconsistent
    # file returns a 400 rather than leaving a half-imported adventure with a
    # gap in its story.
    story = bundle.plan(payload, version)
    # Count again, this time over what is written. The check above reads the
    # file's own lists, and in a v1 file one turn is one entry that carries its
    # retries in a `variants` array. `plan()` expands that into one row per
    # attempt, because SP4 made every attempt a node. A file of 5,000 turns with
    # ten attempts each therefore passes a 5,000-action cap and writes 50,000
    # rows, well inside the 20 MB body limit. `plan()` has no side effects and
    # the adventure does not exist yet, so this check costs only the planning.
    limits.check_bundle_lists(
        actions=story["nodes"],
        memories=story["memories"],
        branches=story["branches"],
    )

    # A raw-dict import bypasses the schemas, so truncate strings bound for
    # VARCHAR columns. Postgres enforces the widths. See `schemas.py`.
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

    # The anchors and the legacy counts describe the same boundary in two
    # coordinate systems. Aligning them requires the actions to be queryable,
    # and this is the only point where both exist.
    db.flush()
    db.expire(adventure, ["actions"])
    bundle.settle(db, adventure, story)

    db.commit()
    db.refresh(adventure)
    # This is not a funnel step. A returning player imports a bundle, so it
    # says nothing about how far a first-time visitor got. It is counted anyway,
    # because it is the clearest evidence that anyone uses the export format.
    analytics.record_event(analytics.EV_IMPORT, user)
    return adventure


# ---------- Adventure scripts ----------

# Fields that are copied from a library Script into its adventure-script
# snapshot, and compared to decide whether a copy is out of date.
SYNC_FIELDS = ("name", "description", "library_js", "input_js", "context_js", "output_js")


def resolve_library_script(
    adv_script: models.AdventureScript, db: Session, user: models.User
) -> models.Script | None:
    """Returns the library Script an adventure script can re-sync from.

    The result is the script this copy was made from. For a legacy copy with no
    link, it is one of the player's own scripts with the same name. Only the
    player's own scripts are considered, so a copy derived from a demo scenario
    has nothing to sync to.
    """
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
    """Attaches a transient `out_of_date` flag, which `AdventureScriptOut` reads.

    The flag is `True` or `False` when a syncable library version exists, and
    `None` when none exists.
    """
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
    """Overwrites this copy's code with the latest from its library script.

    `enabled`, `position`, and the adventure's shared `script_state` are kept.
    """
    get_adventure_or_404(adventure_id, db, user)
    script = db.get(models.AdventureScript, adv_script_id)
    if script is None or script.adventure_id != adventure_id:
        raise HTTPException(404, "Script not found")
    library = resolve_library_script(script, db, user)
    if library is None:
        raise HTTPException(404, "No library script to sync from")
    for field in SYNC_FIELDS:
        setattr(script, field, getattr(library, field))
    # Store the link, so that a name-matched legacy copy syncs by id next
    # time.
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
# An adventure copies the scenario's plot text and story cards when it is
# created, so that later authoring does not change a story in progress. The
# per-script "Sync from library" above works the same way. This section is the
# explicit opt-out. It copies the scenario's current content over the
# adventure's copy.


def resolve_source_scenario(
    adventure: models.Adventure, db: Session, user: models.User
) -> models.Scenario | None:
    """Returns the scenario an adventure can refresh from.

    The result is the scenario the adventure was started from, if that scenario
    still exists and the user can still read it, which means the user owns it or
    it is public. The result is `None` after the scenario is deleted, which sets
    `scenario_id` to NULL, or after it stops being shared.
    """
    if adventure.scenario_id is None:
        return None
    scenario = db.get(models.Scenario, adventure.scenario_id)
    if scenario is None or (scenario.user_id != user.id and not scenario.is_public):
        return None
    return scenario


def _placeholder_names(*texts: str) -> list[str]:
    """Returns the unique `${Placeholder}` names across the given texts.

    The order is first appearance first. This matches the frontend's
    `extractPlaceholders`.
    """
    names: list[str] = []
    for text in texts:
        for match in PLACEHOLDER_RE.finditer(text or ""):
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
    return names


def scenario_placeholder_names(scenario: models.Scenario) -> list[str]:
    """Returns every placeholder the scenario's refreshable content asks for.

    The opening prompt is excluded, because a refresh never rewrites it.
    """
    texts = [scenario.memory, scenario.authors_note, scenario.ai_instructions]
    for card in scenario.story_cards:
        texts += [card.keys, card.entry]
    for ndef in (scenario.stat_schema or {}).get("npcs", {}).values():
        if isinstance(ndef, dict):
            texts += [str(ndef.get("keys") or ""), str(ndef.get("desc") or "")]
    return _placeholder_names(*texts)


def _scenario_cards(adventure: models.Adventure) -> dict[str, models.StoryCard]:
    """Returns the adventure's scenario-derived cards, keyed by `source_ref`.

    An adventure created before `source_ref` existed has none, so it falls back
    to matching the scenario's cards by name. The fallback runs only when the
    adventure has no tagged cards at all. Otherwise a player-authored card that
    shares a scenario card's name would be adopted and overwritten.
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
    """Computes what a refresh would change, without modifying anything.

    The return value is `(plan, specs, matched)`. `plan` is the summary the UI
    shows, `specs` holds the scenario's card specs by ref, and `matched` holds
    the existing adventure card for each ref that already has one.
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
    # Only a card the scenario produced is removable. A player-authored card
    # has no `source_ref` and is never modified.
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
    """Returns what "Update from scenario" would change, for the confirm dialog."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    scenario = resolve_source_scenario(adventure, db, user)
    if scenario is None:
        raise HTTPException(404, "No scenario to update from")
    stored = adventure.placeholders if isinstance(adventure.placeholders, dict) else {}
    plan, _, _ = plan_refresh(adventure, scenario, stored)
    # An adventure started before placeholder answers were stored has none, and
    # an author can add a new `${...}` later. In both cases the player is asked
    # for the missing names, and the answers are saved for next time.
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
    """Copies the scenario's current plot text, story cards, and stat schema over
    this adventure's copy.

    The refresh overwrites the plot fields and every scenario-derived card, adds
    what the scenario gained, and removes what it dropped.

    The refresh leaves these unchanged: the opening `start` action, because the
    story is built on it and it is already part of the memories and the summary;
    the adventure's own title; its story summary; its player-authored story
    cards; and, through `worldstate.reconcile`, the live value of every stat the
    schema still defines.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    scenario = resolve_source_scenario(adventure, db, user)
    if scenario is None:
        raise HTTPException(404, "No scenario to update from")

    values = {**(adventure.placeholders if isinstance(adventure.placeholders, dict) else {}),
              **payload.placeholders}

    # A refresh rewrites the same state a turn is part-way through changing, so
    # it takes the turn slot rather than run at the same time as the
    # generator.
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
            # Store the ref, so that a name-matched legacy card syncs by id
            # next time.
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
    """Returns what the app would send to the AI if the player continued now."""
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
    adventure = get_adventure_or_404(adventure_id, db, user)
    # Name the columns in a query rather than walk `adventure.memories`.
    # Retrieval used to walk the relationship, which is why a turn cost
    # megabytes: a relationship load returns whole entities, so it reads
    # whatever the model carries. `embedding_blob` is deferred and would stay
    # out today, so this rule is about the next wide column rather than that
    # one.
    #
    # The drawer shows the same bank the model reads. The filter uses the same
    # clause retrieval uses, so the drawer answers one question rather than two.
    # An adventure-wide list would show memories from branches this story never
    # went down, which are never retrieved, and a reader cannot tell those apart
    # from the ones in play. No memory becomes unreachable, because a memory
    # belongs to a branch: switching to that branch shows it, and deleting the
    # branch deletes its memories.
    return (
        db.query(models.Memory)
        .options(load_only(*MEMORY_LIST_COLUMNS))
        .filter(
            models.Memory.adventure_id == adventure_id,
            lineage.path_of(db, adventure).clause(models.Memory),
        )
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
    """Adds a memory manually. The next post-turn pass embeds it."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.check_row_cap("memories", db, user, adventure=adventure)
    if not payload.text.strip():
        raise HTTPException(400, "Memory text cannot be empty")
    memory = models.Memory(adventure_id=adventure.id, text=payload.text.strip())
    # No node produced this memory, so it gets a branch but no depth.
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
        memorybank.set_vector(memory, None)  # Re-embed on the next post-turn pass.
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
    """Returns a page of the story, working backwards from the newest action.

    `before_id` is the oldest action the caller already holds, so scrolling up
    asks for what comes before it. Omit `before_id` for the newest window. See
    `action_window` for why this anchors on a row rather than an offset.
    """
    adventure = get_adventure_or_404(adventure_id, db, user)
    limit = max(1, min(limit, ACTION_PAGE * 4))
    actions, total, has_more = action_window(
        db, adventure, before_id=before_id, limit=limit
    )
    return schemas.ActionPage(
        actions=[
            schemas.ActionOut.model_validate(a)
            for a in annotate_takes(db, adventure.id, actions)
        ],
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
    # One row holds one text. Nothing mirrors it now, so nothing else has to be
    # updated. The edit used to have to be written into the live variant entry
    # as well, or paging away and back reverted it.
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
    # This works like undo. The turn is deleted with all of its attempts, and
    # whatever it produced is withdrawn. Nothing else is needed, because the
    # marks are depths, and a depth does not move when an action before it is
    # deleted.
    delete_turn(db, adventure, action)
    db.flush()
    db.expire(adventure, ["actions"])
    # Deleting the newest action moves the tip. Deleting an action in the
    # middle leaves a gap in the depths, which is intended. See
    # `_backfill_tree`.
    tree.refresh_head(db, adventure)
    db.commit()
