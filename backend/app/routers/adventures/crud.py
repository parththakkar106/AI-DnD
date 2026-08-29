"""Listing, creating, reading, renaming, and deleting adventures.

The world-state and script-state readers are here too, because they report an
adventure's stored state rather than play a turn.
"""

from fastapi import Body, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ... import analytics, attempts, images, limits, memorybank, models, schemas, tree, worldstate
from ...database import get_db

from .deps import CurrentUser, current_adventure, router
from .paging import action_window, annotate_takes
from .scenario_text import fill_placeholders, scenario_card_specs


# How many characters of the last narration a Continue card shows. The limit is
# long enough to re-establish the scene and short enough to keep the card
# small.
SNIPPET_MAX = 220


def _snippet(text: str) -> str:
    """Condenses stored action text into a single line for a card."""
    # `turns._generate_turn` strips the world-state block before storing AI text,
    # so this function only has to normalize whitespace.
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
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Returns the adventure and the newest window of its story.

    `actions` holds the last `ACTION_PAGE` actions, not all of them.
    `action_count` reports the real total, so the reader knows that more actions
    exist above. `GET /{id}/actions` serves the older pages as the reader
    scrolls up.
    """
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
    adventure: models.Adventure = Depends(current_adventure),
):
    """Returns the scripting `state` object.

    The object holds every variable that scripts read and write through
    `state.x`, persisted after each hook. It stays `{}` until a script sets a
    variable.
    """
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    return {"state": state}


@router.get("/{adventure_id}/world-state")
def get_world_state(
    adventure: models.Adventure = Depends(current_adventure),
):
    """Returns the live RPG world state and the scenario's `stat_schema`.

    The play view uses both to render the character sheet and the milestones.
    `schema` is null when the adventure has no RPG layer.
    """
    schema = adventure.scenario.stat_schema if adventure.scenario else None
    state = adventure.world_state if isinstance(adventure.world_state, dict) else {}
    return {
        "state": state,
        "schema": schema if worldstate.has_schema(schema) else None,
    }


@router.put("/{adventure_id}/world-state")
def override_world_state(
    overrides: dict = Body(...),
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    """Edits the live RPG values directly, as a manual correction rather than a turn.

    `overrides` maps paths such as `player.hp`, `npc.gwen.trust`, `flags.x`, and
    `milestones.y` to their new absolute values. The endpoint rejects unknown
    paths and wrong types one at a time, and applies the rest.
    """
    schema = adventure.scenario.stat_schema if adventure.scenario else None
    if not worldstate.has_schema(schema):
        raise HTTPException(400, "This adventure has no RPG world-state layer")
    state = adventure.world_state if isinstance(adventure.world_state, dict) else {}
    new_state, report = worldstate.apply_override(state, schema, overrides)
    adventure.world_state = new_state
    db.commit()
    return {"state": new_state, "report": report}


@router.patch("/{adventure_id}", response_model=schemas.AdventureOut)
def update_adventure(
    payload: schemas.AdventureUpdate,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(adventure, field, value)
    db.commit()
    return adventure


@router.delete("/{adventure_id}", status_code=204)
def delete_adventure(
    adventure_id: int,
    db: Session = Depends(get_db),
    adventure: models.Adventure = Depends(current_adventure),
):
    db.delete(adventure)
    db.commit()
    # No later request reads this adventure's vectors, so drop them now. The
    # cache would otherwise hold them until the process restarted.
    memorybank.forget_cached_vectors(adventure_id)
