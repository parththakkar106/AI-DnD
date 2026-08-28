"""Copying a scenario's text and story cards onto an adventure.

An adventure holds its own copy of the scenario's plot text and cards, so that
later authoring does not change a story in progress. Two callers make that copy:
`crud.create_adventure` on the way in, and `refresh` when the player asks for the
scenario's current content.
"""
import re


from ... import models, worldstate


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
