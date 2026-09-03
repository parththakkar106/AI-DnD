"""Revising the changes a turn made to world state, after the turn has played.

The AI proposes a delta each turn and `worldstate.apply_delta` decides what it is
allowed to do. Sometimes the proposal is simply wrong — a scratch that cost 40 hp,
a trust swing nobody earned, a milestone the scene did not reach — and the player
wants a different number without rewriting the narration.

This module replays that turn's delta with the player's numbers in place of the
AI's. It is a replay, not a patch: the world state is rewound to what the turn
started from and the corrected delta is put through the same referee, so every
limit the AI is held to holds here too. A player who wants to sidestep the limits
wants `PUT /world-state`, which sets values outright and is a different act.

Only the newest turn can be revised. A delta halfway up the story is the input to
every state that followed it, and re-running one would leave those states
describing a turn that no longer happened.
"""

import copy

from fastapi import Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import attempts, models, schemas, worldstate
from ...database import get_db

from . import turns
from .deps import current_adventure, router
from .nodes import db_tip


def _schema_or_400(adventure: models.Adventure) -> dict:
    schema = adventure.scenario.stat_schema if adventure.scenario else None
    if not worldstate.has_schema(schema):
        raise HTTPException(400, "This adventure has no RPG world-state layer")
    return schema


def _ai_action_or_404(
    db: Session, adventure: models.Adventure, action_id: int
) -> models.Action:
    action = db.get(models.Action, action_id)
    if action is None or action.adventure_id != adventure.id:
        raise HTTPException(404, "Action not found")
    if action.type != "ai":
        raise HTTPException(400, "Only the AI's turns change world state")
    return action


def _state_before(
    db: Session, adventure: models.Adventure, action: models.Action, schema: dict
) -> dict:
    """Returns the world state this turn was played from.

    The preceding node carries the state it left behind, which is this turn's
    starting position (see `models.Action.world_state_after`). Two cases fall
    back to a fresh instantiation: the turn is the first one in the story, so
    nothing precedes it, and the preceding row predates SP4 and never recorded an
    outcome. Both mean the same thing here — there is no recorded position to
    rewind to, and the schema's initial values are the only defensible one.

    The starting state carries `_meta.last_changed`, which is the cooldown clock,
    so a revision is held to the same cooldowns the turn itself was.
    """
    before = attempts.preceding(db, adventure, action)
    if before is not None and isinstance(before.world_state_after, dict):
        return copy.deepcopy(before.world_state_after)
    return worldstate.instantiate(schema)


def _is_the_newest_turn(db: Session, adventure: models.Adventure,
                        action: models.Action) -> bool:
    tip = db_tip(db, adventure)
    return tip is not None and tip.id == action.id


@router.get("/{adventure_id}/actions/{action_id}/world-delta")
def get_world_delta(
    action_id: int,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Returns the delta this turn proposed, and what the referee did with it.

    `delta` is the editable object: the paths the turn asked to change, each
    holding the change it asked for, in the shape the AI sends and the revision
    endpoint takes back. `applied` is what actually landed, path by path, so the
    editor can show a number the referee reduced beside the number that was asked
    for. `editable` says whether this turn is still the newest one, which is the
    only turn a revision may touch.

    This is a separate read rather than a field on the action, because it is
    wanted only when the editor opens. `world_changes`, which every page load
    carries, is a rendering and has no paths in it.
    """
    _schema_or_400(adventure)
    action = _ai_action_or_404(db, adventure, action_id)
    wd = action.world_delta if isinstance(action.world_delta, dict) else {}
    return {
        "delta": wd.get("delta") or {},
        "applied": wd.get("applied") or [],
        "clamped": wd.get("clamped") or [],
        "rejected": wd.get("rejected") or [],
        "revised": bool(wd.get("revised")),
        "editable": _is_the_newest_turn(db, adventure, action),
    }


@router.put("/{adventure_id}/actions/{action_id}/world-delta")
def revise_world_delta(
    action_id: int,
    delta: dict = Body(...),
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Replays the newest turn's state changes with `delta` in place of the AI's.

    `delta` is the whole corrected object, not a patch of the stored one, in the
    same shape the AI emits: `{"player.hp": -5, "flags.alarm": true}`. A path left
    out of it is a change removed, a path added is a change the turn should have
    made, and a value changed is the same change at a different size. An empty
    object means the turn changed nothing.

    The turn's own depth is passed to the referee, because the cooldown rules are
    about a position in the story and a revision does not move the turn. The
    result is the same three lists a turn produces, so a revision that overshoots
    a limit is clamped and reported exactly as the AI's would have been.
    """
    schema = _schema_or_400(adventure)
    action = _ai_action_or_404(db, adventure, action_id)
    if not isinstance(delta, dict):
        raise HTTPException(422, "The delta must be an object of path → change")
    if not _is_the_newest_turn(db, adventure, action):
        raise HTTPException(
            409,
            "Only the newest turn's changes can be revised. Later turns were "
            "played from the state this one left behind.",
        )

    # Held for the reason undo and delete hold it: this endpoint rewrites the
    # shared world state, and a turn that is still generating is about to write
    # it from a position this call is about to change.
    turns.acquire_turn_lock(adventure.id)
    try:
        new_state, report = worldstate.apply_delta(
            _state_before(db, adventure, action, schema),
            schema,
            delta,
            action.depth or 0,
        )
        adventure.world_state = new_state
        # The turn's outcome is what the story now stands on, so the node has to
        # carry the revised state and not the one the AI produced. A later undo,
        # delete, or take switch restores this node's outcome, and leaving the
        # old one here would put the AI's numbers back.
        action.world_state_after = copy.deepcopy(new_state)
        action.world_delta = {
            "delta": delta,
            "applied": report.get("applied") or [],
            "clamped": report.get("clamped") or [],
            "rejected": report.get("rejected") or [],
            # Marks the turn as the player's correction rather than the AI's
            # proposal. Nothing in the engine reads it; the editor shows it, and
            # it is the only record that these numbers were not the model's.
            "revised": True,
        }
        # Insights replays the stored prompt for a turn, and its world-state
        # panel reads this slice. Leaving the AI's delta there would have the
        # viewer explain the turn with numbers the story no longer contains.
        # Touching the attribute loads the deferred column, which is why this
        # runs on one action in one deliberate request and nowhere else.
        snapshot = action.context_snapshot
        if isinstance(snapshot, dict) and isinstance(snapshot.get("world_state"), dict):
            action.context_snapshot = snapshot | {
                "world_state": {"delta": delta, "report": report, "state": new_state}
            }
        adventure.updated_at = models.utcnow()
        db.commit()
    finally:
        turns._active_turns.discard(adventure.id)

    db.refresh(action)
    return {
        "state": new_state,
        "report": report,
        "action": schemas.ActionOut.model_validate(action),
    }
