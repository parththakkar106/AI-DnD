"""Copying a scenario's current content over an adventure's copy.

An adventure copies the scenario's plot text and story cards when it is created,
so that later authoring does not change a story in progress. The per-script "Sync
from library" in `scripts` works the same way. This module is the explicit
opt-out. The preview endpoint reports what would change, and the write endpoint
applies it.
"""

from fastapi import Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import models, schemas, worldstate
from ...database import get_db

from . import turns
from .deps import CurrentUser, get_adventure_or_404, router
from .scenario_text import (
    CARD_FIELDS, SCENARIO_TEXT_FIELDS, fill_placeholders, scenario_card_specs,
    scenario_placeholder_names,
)


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
    turns.acquire_turn_lock(adventure_id)
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
        turns._active_turns.discard(adventure_id)

    db.refresh(adventure)
    return adventure
