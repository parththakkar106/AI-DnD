"""The pre-played adventure every new guest is given: app/starter.py.

The starter is a shipped export bundle, so the two things that can break it are
the file and the import path. A bundle that no longer plans, or a story trimmed
to a state a player cannot continue from, both leave the guest worse off than an
empty account would.

    python -m pytest tests/test_starter_adventure.py -v
"""
import json
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import bundle, models, seed, starter
from app.database import Base
from app.migrations import bootstrap


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}",
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    bootstrap(engine)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def guest(db):
    user = models.User(is_guest=True)
    db.add(user)
    db.commit()
    return user


def payload() -> dict:
    return json.loads(starter.STARTER_FILE.read_text(encoding="utf-8"))


def test_the_shipped_file_is_a_bundle_this_build_can_import():
    """The file is written by an export, so a format change can strand it."""
    data = payload()
    version = bundle.check_format(data)
    assert version == bundle.FORMAT
    story = bundle.plan(data, version)
    assert story["nodes"]


def test_a_guest_gets_the_adventure_and_owns_it(db, guest):
    adventure = starter.give(db, guest)
    db.commit()
    assert adventure is not None
    assert adventure.user_id == guest.id
    assert "Pokemon" in adventure.title
    assert len(adventure.actions) == len(payload()["actions"])


def test_the_story_cards_come_with_it(db, guest):
    """A copy without the cards would drop out of character on the next turn."""
    adventure = starter.give(db, guest)
    db.commit()
    assert {c.name for c in adventure.story_cards} == {
        c["name"] for c in payload()["storyCards"]
    }


def test_the_turns_carry_what_the_engine_recorded(db, guest):
    """The point of shipping a played story: the summaries have chips in them.

    An applied change, a refused one, and a milestone all appear in the first
    two exchanges, which is what a visitor sees before spending a demo turn.
    """
    adventure = starter.give(db, guest)
    db.commit()
    chips = [chip for action in adventure.actions for chip in action.world_changes]
    kinds = {chip["kind"] for chip in chips}
    assert "stat" in kinds
    assert "rejected" in kinds
    assert "milestone" in kinds


def test_the_state_left_behind_can_be_played_from(db, guest):
    """The opponent has to be on the field with HP, or every hit is refused.

    A Pokemon sent in at 0 HP sits at the floor of `active_hp`, so the engine
    refuses each later change and the story stops moving. That state reached a
    playtest once, and it must not be what a guest inherits.
    """
    adventure = starter.give(db, guest)
    db.commit()
    milo = adventure.world_state["npc"]["milo"]
    assert milo["active_hp"] > 0
    assert milo["active_pokemon"]
    assert adventure.actions[-1].world_state_after["npc"]["milo"] == milo


def test_a_missing_file_costs_the_guest_nothing(db, guest, monkeypatch):
    """A packaging mistake must not stop an account from being created."""
    monkeypatch.setattr(starter, "STARTER_FILE", starter.STARTER_FILE.with_name("gone.json"))
    assert starter.give(db, guest) is None


def test_a_broken_bundle_leaves_no_half_written_adventure(db, guest, monkeypatch):
    """The savepoint: a failure partway through discards the rows it wrote."""
    monkeypatch.setattr(bundle, "materialize",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert starter.give(db, guest) is None
    db.commit()
    assert db.query(models.Adventure).count() == 0


def test_the_copy_inherits_the_demo_scenario_art(db, guest):
    """An adventure has no cover art of its own; it inherits the scenario's.

    A bundle carries no scenario id, so the link is made by title. Without it
    the starter card shows a monogram while the demo it came from shows its
    artwork.
    """
    title = payload()["scenarioTitle"]
    data = json.loads((seed.SEED_DIR / "05-league-championship.json").read_text(encoding="utf-8"))
    # The demo the copy came from, as the seeder would have written it. The
    # seeder itself opens its own session against the app's engine, so it
    # cannot be pointed at this fixture's database.
    assert data["title"] == title, "the starter names a scenario no seed file ships"
    db.add(models.Scenario(user_id=None, is_public=True, title=title, image=data["image"]))
    db.commit()

    adventure = starter.give(db, guest)
    db.commit()
    scenario = seed.find_seeded(db, title)
    assert scenario is not None and scenario.image
    assert adventure.scenario_id == scenario.id


def test_a_missing_demo_scenario_only_costs_the_art(db, guest):
    """Nothing seeds the scenarios in this fixture, so the link finds nothing."""
    adventure = starter.give(db, guest)
    db.commit()
    assert adventure is not None
    assert adventure.scenario_id is None
