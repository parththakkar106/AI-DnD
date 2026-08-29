"""Playing a turn: the model call, the SSE stream, and the one-turn-at-a-time lock.

Everything a test needs to intercept lives here, and other modules reach it as
`turns.<name>` rather than importing it by value. That matters twice. The turn
lock guards one set only while one module owns it. And a test that replaces
`OpenAICompatibleProvider`, `generate_turn`, or `check_demo_cap` patches this
module, which every caller reads through.
"""
import threading

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ... import (
    analytics, attempts, auth, limits, memorybank, models, schemas, tree, worldstate,
)
from ...context import build_context, cursors
from ...database import get_db
from ...providers import OpenAICompatibleProvider, PromptParts, ProviderError
from ...scripting import ScriptPipeline
from ...sse import SSE_HEADERS, sse, turn_error
from ..settings import get_settings

from .deps import CurrentUser, current_adventure, router
from .nodes import _move_to_after, next_depth
from .paging import annotate_takes


def world_delta_of(snapshot: dict | None) -> dict | None:
    """Returns the bulk-read slice of a context snapshot, for `Action.world_delta`.

    `context_snapshot` is deferred because it holds the whole assembled prompt.
    The parts that every action needs get their own small column instead: the
    world-change chips, the emit block replayed into history, and the refusal
    note fed back to the model. Update this function wherever a snapshot is
    written.

    Carry all three report lists, not just `applied`. `Action.world_changes`
    marks a chip from `clamped` and builds its refusal chips from `rejected`,
    and `worldstate.refusals` reads both. Storing `applied` alone left every
    consumer unable to tell a refused change from one that worked, which is the
    distinction this column exists to carry. The two extra lists are subsets of
    one turn's block, so they cost a few hundred bytes per action at most.
    """
    ws = (snapshot or {}).get("world_state")
    if not isinstance(ws, dict):
        return None
    report = ws.get("report") or {}
    return {
        "delta": ws.get("delta") or {},
        "applied": report.get("applied") or [],
        "clamped": report.get("clamped") or [],
        "rejected": report.get("rejected") or [],
    }


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
        # Flush so the new attempt has an id. The session does not autoflush,
        # and attempts page in id order, so a read taken before this point puts
        # the newest attempt nowhere.
        db.flush()
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
        # `build_context` for the AI action does not see the player action
        # that was just saved.
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
    adventure: models.Adventure = Depends(current_adventure),
):
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
