"""Seed public demo scenarios on startup.

Every JSON file in ``seed_data/`` describes one demo scenario in the same
model-native shape the export endpoint produces. Seeded scenarios have a NULL
owner and ``is_public=True``, so every visitor (including guests) sees them and
can start an adventure from them, while nobody can edit them. Starting an
adventure copies the scenario's story cards and scripts into the adventure, so
the seeded scripts run for guests too.

Seed files are the source of truth for demo content: a scenario is inserted if
missing, and reconciled in place when a seed file's content changes (so edits
ship on the next deploy). When a seed already matches, nothing is written, so
this stays cheap to run on every boot. Existing adventures already started from
a demo keep their own copied cards/scripts and are unaffected — only new
adventures pick up the updated content.
"""

import json
import logging
from pathlib import Path

from sqlalchemy.engine import Engine

from . import models
from .database import SessionLocal

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parent / "seed_data"

_SCALARS = ("description", "prompt", "memory", "authors_note", "ai_instructions", "tags")
_CARD_FIELDS = ("type", "name", "keys", "entry", "notes")
_SCRIPT_FIELDS = ("name", "library_js", "input_js", "context_js", "output_js")


def seed_public_scenarios(engine: Engine) -> None:
    if not SEED_DIR.is_dir():
        return
    files = sorted(SEED_DIR.glob("*.json"))
    if not files:
        return

    db = SessionLocal()
    try:
        changed = 0
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping seed file %s: %s", path.name, exc)
                continue

            title = (data.get("title") or "").strip()
            if not title:
                continue

            existing = (
                db.query(models.Scenario)
                .filter(
                    models.Scenario.title == title,
                    models.Scenario.user_id.is_(None),
                    models.Scenario.is_public.is_(True),
                )
                .first()
            )
            if existing is None:
                _insert_scenario(db, data)
                changed += 1
            elif not _matches(existing, data):
                _update_scenario(db, existing, data)
                changed += 1

        if changed:
            db.commit()
            logger.info("Seeded/updated %d public demo scenario(s).", changed)
    except Exception:
        db.rollback()
        # A seed failure must never take the app down; log and carry on.
        logger.exception("Seeding public scenarios failed; continuing without them.")
    finally:
        db.close()


def _card_tuple(source, get) -> tuple:
    return tuple(get(source, f) for f in _CARD_FIELDS)


def _script_tuple(source, get) -> tuple:
    return tuple(get(source, f) for f in _SCRIPT_FIELDS)


def _matches(scenario: models.Scenario, data: dict) -> bool:
    """True when the DB scenario already equals the seed file, so we can skip
    the write and avoid churning rows on every boot."""
    if any(getattr(scenario, f) != data.get(f, "") for f in _SCALARS):
        return False
    if (scenario.stat_schema or None) != (data.get("stat_schema") or None):
        return False
    have_cards = sorted(_card_tuple(c, lambda o, f: getattr(o, f)) for c in scenario.story_cards)
    want_cards = sorted(
        _card_tuple(c, lambda o, f: o.get(f, ""))
        for c in (data.get("story_cards") or []) if isinstance(c, dict)
    )
    if have_cards != want_cards:
        return False
    have_scripts = sorted(_script_tuple(s, lambda o, f: getattr(o, f)) for s in scenario.scripts)
    want_scripts = sorted(
        _script_tuple(s, lambda o, f: (o.get(f, "") or ("Script" if f == "name" else "")))
        for s in (data.get("scripts") or []) if isinstance(s, dict)
    )
    return have_scripts == want_scripts


def _insert_scenario(db, data: dict) -> None:
    scenario = models.Scenario(user_id=None, is_public=True, title=data.get("title", ""))
    _apply_scalars(scenario, data)
    db.add(scenario)
    db.flush()
    _populate_children(db, scenario, data)


def _update_scenario(db, scenario: models.Scenario, data: dict) -> None:
    _apply_scalars(scenario, data)
    # Replace child content wholesale — demo content is server-owned and cheap
    # to rebuild, and this keeps the scenario row (and adventure FKs) intact.
    for card in list(scenario.story_cards):
        db.delete(card)
    for script in list(scenario.scripts):
        db.delete(script)
    scenario.scripts = []
    db.flush()
    _populate_children(db, scenario, data)


def _apply_scalars(scenario: models.Scenario, data: dict) -> None:
    for field in _SCALARS:
        setattr(scenario, field, data.get(field, ""))
    # Phase 12: RPG world-state template (a JSON dict, not a scalar string).
    scenario.stat_schema = data.get("stat_schema") or None


def _populate_children(db, scenario: models.Scenario, data: dict) -> None:
    for card in data.get("story_cards") or []:
        if not isinstance(card, dict):
            continue
        db.add(
            models.StoryCard(
                scenario_id=scenario.id,
                type=card.get("type", ""),
                name=card.get("name", ""),
                keys=card.get("keys", ""),
                entry=card.get("entry", ""),
                notes=card.get("notes", ""),
            )
        )

    for item in data.get("scripts") or []:
        if not isinstance(item, dict):
            continue
        script = models.Script(
            user_id=None,
            name=item.get("name", "Script"),
            description=item.get("description", ""),
            library_js=item.get("library_js", ""),
            input_js=item.get("input_js", ""),
            context_js=item.get("context_js", ""),
            output_js=item.get("output_js", ""),
        )
        db.add(script)
        db.flush()
        scenario.scripts.append(script)
