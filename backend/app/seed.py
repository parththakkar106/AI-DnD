"""Seed public demo scenarios on startup.

Every JSON file in ``seed_data/`` describes one demo scenario in the same
model-native shape the export endpoint produces. Seeded scenarios have a NULL
owner and ``is_public=True``, so every visitor (including guests) sees them and
can start an adventure from them, while nobody can edit them. Starting an
adventure copies the scenario's story cards and scripts into the adventure, so
the seeded scripts run for guests too.

Seed files are the source of truth for demo content: a scenario is inserted if
missing, reconciled in place when a seed file's content changes, and deleted
when no file claims its title any more, so an edit ships on the next deploy.
Rename a seed by changing its `title` and listing the old one under
`previous_titles`, which moves the rename onto the existing row. When a seed already matches, nothing is written, so
this stays cheap to run on every boot. An adventure already started from a demo
keeps its own copied cards and scripts and is unchanged. Only a new adventure
picks up the updated content.
"""

import json
import logging
from pathlib import Path

from sqlalchemy.engine import Engine

from . import models
from .database import SessionLocal

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parent / "seed_data"

# `title` is in this list so that a rename found through `previous_titles` is
# both detected by `_matches` and written by `_apply_scalars`.
_SCALARS = ("title", "description", "prompt", "memory", "authors_note", "ai_instructions",
            "tags", "image", "icon")
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
        # Every title the files claim, including the ones they used to use. The
        # sweep below deletes the seeded rows this set does not name.
        claimed: set[str] = set()
        complete = True
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping seed file %s: %s", path.name, exc)
                complete = False
                continue

            title = (data.get("title") or "").strip()
            if not title:
                continue

            claimed.add(title)
            claimed.update(str(old) for old in data.get("previous_titles") or [])

            existing = find_seeded(db, title) or _find_renamed(db, data)
            if existing is None:
                _insert_scenario(db, data)
                changed += 1
            elif not _matches(existing, data):
                _update_scenario(db, existing, data)
                changed += 1

        changed += _sweep_unclaimed(db, claimed) if complete else 0
        if changed:
            db.commit()
            logger.info("Seeded/updated %d public demo scenario(s).", changed)
    except Exception:
        db.rollback()
        # A seed failure must never take the app down; log and carry on.
        logger.exception("Seeding public scenarios failed; continuing without them.")
    finally:
        db.close()


def _sweep_unclaimed(db, claimed: set[str]) -> int:
    """Deletes seeded scenarios no seed file claims any more, and returns how
    many went.

    A rename used to strand the row it left behind. `previous_titles` stops new
    ones appearing, and this removes the ones already out there, which was
    otherwise hand-work on every deployment. Only rows with a NULL owner and
    `is_public` are considered, and a player's own scenario is neither, so
    nothing anybody created can be reached from here.

    An adventure started from a deleted demo survives. `adventures.scenario_id`
    is `ON DELETE SET NULL`, so the story, its cards, and its scripts are its
    own copies and stay; the adventure loses the cover art it inherited.

    The caller skips this when a seed file failed to parse. A file that cannot
    be read claims no title, and deleting on that basis would treat a syntax
    error as an instruction to remove live content. An empty seed directory
    never reaches here at all, for the same reason.
    """
    stale = (
        db.query(models.Scenario)
        .filter(
            models.Scenario.user_id.is_(None),
            models.Scenario.is_public.is_(True),
            models.Scenario.title.notin_(claimed) if claimed else True,
        )
        .all()
    )
    for scenario in stale:
        logger.info("Removing seeded scenario %r; no seed file claims it.", scenario.title)
        # The scripts are joined through a secondary table, so nothing cascades
        # to them. They have a NULL owner and no other reader.
        for script in list(scenario.scripts):
            db.delete(script)
        scenario.scripts = []
        db.delete(scenario)
    return len(stale)


def _card_tuple(source, get) -> tuple:
    return tuple(get(source, f) for f in _CARD_FIELDS)


def _script_tuple(source, get) -> tuple:
    return tuple(get(source, f) for f in _SCRIPT_FIELDS)


def find_seeded(db, title: str) -> models.Scenario | None:
    """Returns the seeded scenario with this exact title, if there is one."""
    return (
        db.query(models.Scenario)
        .filter(
            models.Scenario.title == title,
            models.Scenario.user_id.is_(None),
            models.Scenario.is_public.is_(True),
        )
        .first()
    )


def _find_renamed(db, data: dict) -> models.Scenario | None:
    """Returns the row a renamed seed file used to own, so the rename lands on it.

    A seed is matched by title, so renaming one inserts a second scenario and
    strands the first. The stranded row stays public forever and has to be
    deleted by hand on every deployment. List the old title under
    `previous_titles` in the seed file and the rename updates the existing row
    instead, which also keeps the adventures already started from it pointing at
    a scenario that still exists.

    Drop a `previous_titles` entry once every deployment has booted past it.
    """
    for old in data.get("previous_titles") or []:
        found = find_seeded(db, str(old))
        if found is not None:
            return found
    return None


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
    # Replace the child content in full. Demo content is owned by the server and
    # cheap to rebuild, and replacing it this way keeps the scenario row, and the
    # adventure foreign keys that point at it, intact.
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
