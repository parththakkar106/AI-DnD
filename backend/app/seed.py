"""Seed public demo scenarios on startup.

Every JSON file in ``seed_data/`` describes one demo scenario in the same
model-native shape the export endpoint produces. Seeded scenarios have a NULL
owner and ``is_public=True``, so every visitor (including guests) sees them and
can start an adventure from them, while nobody can edit them. Starting an
adventure copies the scenario's story cards and scripts into the adventure, so
the seeded scripts run for guests too.

The seed is idempotent: a scenario is inserted only if no public, unowned
scenario with the same title already exists, so it is safe to run on every boot
and it re-heals a database that lost its demo content.
"""

import json
import logging
from pathlib import Path

from sqlalchemy.engine import Engine

from . import models
from .database import SessionLocal

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parent / "seed_data"


def seed_public_scenarios(engine: Engine) -> None:
    if not SEED_DIR.is_dir():
        return
    files = sorted(SEED_DIR.glob("*.json"))
    if not files:
        return

    db = SessionLocal()
    try:
        created = 0
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping seed file %s: %s", path.name, exc)
                continue

            title = (data.get("title") or "").strip()
            if not title:
                continue

            exists = (
                db.query(models.Scenario)
                .filter(
                    models.Scenario.title == title,
                    models.Scenario.user_id.is_(None),
                    models.Scenario.is_public.is_(True),
                )
                .first()
            )
            if exists is not None:
                continue

            _insert_scenario(db, data)
            created += 1

        if created:
            db.commit()
            logger.info("Seeded %d public demo scenario(s).", created)
    except Exception:
        db.rollback()
        # A seed failure must never take the app down; log and carry on.
        logger.exception("Seeding public scenarios failed; continuing without them.")
    finally:
        db.close()


def _insert_scenario(db, data: dict) -> None:
    scenario = models.Scenario(
        user_id=None,
        is_public=True,
        title=data.get("title", ""),
        description=data.get("description", ""),
        prompt=data.get("prompt", ""),
        memory=data.get("memory", ""),
        authors_note=data.get("authors_note", ""),
        ai_instructions=data.get("ai_instructions", ""),
        tags=data.get("tags", ""),
    )
    db.add(scenario)
    db.flush()

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
