"""Seeded demo scenarios: renames land in place, and orphans are removed.

A seed file is matched to its scenario by title, so renaming one used to insert
a second scenario and leave the first public forever. Both halves of the fix are
here: `previous_titles` moves the rename onto the existing row, and the sweep
deletes seeded rows no file claims any more.

    python -m pytest tests/test_seed_sweep.py -v
"""
import json


import pytest

from app import models, seed
from app.database import Base, SessionLocal, engine


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seed_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "SEED_DIR", tmp_path)
    return tmp_path


def write_seed(seed_dir, name: str, **fields) -> None:
    data = {"title": fields.pop("title"), "description": "d", "prompt": "p"}
    data.update(fields)
    (seed_dir / name).write_text(json.dumps(data), encoding="utf-8")


def titles(db) -> set[str]:
    return {s.title for s in db.query(models.Scenario).all()}


def test_a_seed_is_inserted_once(db, seed_dir):
    write_seed(seed_dir, "a.json", title="Alpha")
    seed.seed_public_scenarios(engine)
    seed.seed_public_scenarios(engine)
    assert titles(db) == {"Alpha"}


def test_a_rename_moves_the_existing_row(db, seed_dir):
    """The whole point: one row, one id, and no orphan left behind."""
    write_seed(seed_dir, "a.json", title="Alpha")
    seed.seed_public_scenarios(engine)
    original = db.query(models.Scenario).one().id

    write_seed(seed_dir, "a.json", title="Alpha Prime", previous_titles=["Alpha"])
    seed.seed_public_scenarios(engine)
    db.expire_all()
    row = db.query(models.Scenario).one()
    assert (row.id, row.title) == (original, "Alpha Prime")


def test_a_seeded_row_no_file_claims_is_deleted(db, seed_dir):
    """The stale demo this sweep exists for, reproduced."""
    write_seed(seed_dir, "a.json", title="Alpha")
    write_seed(seed_dir, "b.json", title="Beta")
    seed.seed_public_scenarios(engine)
    assert titles(db) == {"Alpha", "Beta"}

    (seed_dir / "b.json").unlink()
    seed.seed_public_scenarios(engine)
    db.expire_all()
    assert titles(db) == {"Alpha"}


def test_a_previous_title_still_counts_as_claimed(db, seed_dir):
    """A rename runs the sweep in the same pass, and must not eat its own row."""
    write_seed(seed_dir, "a.json", title="Alpha")
    seed.seed_public_scenarios(engine)
    write_seed(seed_dir, "a.json", title="Alpha Prime", previous_titles=["Alpha"])
    seed.seed_public_scenarios(engine)
    db.expire_all()
    assert titles(db) == {"Alpha Prime"}


def test_a_players_own_scenario_is_never_touched(db, seed_dir):
    """Only a NULL owner and `is_public` make a row seeded. A player's is
    neither, so nothing anybody created is reachable from the sweep."""
    user = models.User(is_guest=True)
    db.add(user)
    db.flush()
    db.add(models.Scenario(user_id=user.id, is_public=True, title="Mine"))
    db.add(models.Scenario(user_id=user.id, is_public=False, title="Also mine"))
    db.commit()

    write_seed(seed_dir, "a.json", title="Alpha")
    seed.seed_public_scenarios(engine)
    db.expire_all()
    assert titles(db) == {"Alpha", "Mine", "Also mine"}


def test_an_unreadable_seed_file_stops_the_sweep(db, seed_dir):
    """A syntax error is not an instruction to delete live demo content.

    A file that will not parse claims no title, so sweeping on that pass would
    remove the scenario it describes, and the next deploy would put it back.
    """
    write_seed(seed_dir, "a.json", title="Alpha")
    write_seed(seed_dir, "b.json", title="Beta")
    seed.seed_public_scenarios(engine)

    (seed_dir / "b.json").write_text("{ not json", encoding="utf-8")
    seed.seed_public_scenarios(engine)
    db.expire_all()
    assert titles(db) == {"Alpha", "Beta"}


def test_an_adventure_outlives_the_demo_it_started_from(db, seed_dir):
    """`adventures.scenario_id` is ON DELETE SET NULL, so the story survives and
    loses only the cover art it inherited."""
    write_seed(seed_dir, "a.json", title="Alpha")
    write_seed(seed_dir, "keep.json", title="Kept")
    seed.seed_public_scenarios(engine)
    scenario = db.query(models.Scenario).filter_by(title="Alpha").one()

    user = models.User(is_guest=True)
    db.add(user)
    db.flush()
    adventure = models.Adventure(user_id=user.id, scenario_id=scenario.id, title="Mine")
    db.add(adventure)
    db.commit()
    adventure_id = adventure.id

    (seed_dir / "a.json").unlink()
    seed.seed_public_scenarios(engine)
    db.expire_all()
    assert titles(db) == {"Kept"}
    survivor = db.get(models.Adventure, adventure_id)
    assert survivor is not None
    assert survivor.scenario_id is None


def test_an_empty_seed_directory_deletes_nothing(db, seed_dir):
    """No files at all reads as a packaging failure, not as a request to remove
    every demo. The seeder returns before the sweep."""
    write_seed(seed_dir, "a.json", title="Alpha")
    seed.seed_public_scenarios(engine)

    (seed_dir / "a.json").unlink()
    seed.seed_public_scenarios(engine)
    db.expire_all()
    assert titles(db) == {"Alpha"}
