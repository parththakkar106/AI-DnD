"""Phase 14 SP9 — a turn's takes are grouped by their parent, not by where they sit.

SP4 made every take a node at the same (branch, depth). That coordinate answers
"which takes belong to this turn" right up until one of them is forked onto its
own branch — at which point it *leaves* the coordinate and reads as the only
take of its turn, with its siblings unreachable from the line it was taken on.

The parent does not move when a branch does, which is the whole of the fix. It
also gets the nesting right for free: takes under C1 and takes under C2 share a
depth and, until one forks, a branch. Only the parent separates them.

And the branch itself is lazy now. Stepping between takes creates nothing —
looking is free. The fork happens on the first thing *written* below a take the
story moved past, which is the first moment the player has said which line they
mean.

    python -m pytest tests/test_take_parentage.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import attempts, auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import PromptParts
from app.routers import adventures


class ScriptedProvider:
    replies: list = []
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        index = min(ScriptedProvider.calls, len(ScriptedProvider.replies) - 1)
        ScriptedProvider.calls += 1
        yield ("text", ScriptedProvider.replies[index])


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="takes@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    adv = models.Adventure(
        user_id=user.id, title="Cave", script_state={}, world_state={}
    )
    setup.add(adv)
    setup.flush()
    setup.add(
        models.Action(adventure_id=adv.id, index=0, type="start", text="You enter.")
    )
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    # Distinct replies so a take can be told apart from its siblings by text.
    ScriptedProvider.replies = [f"Take {n}." for n in range(1, 40)]
    ScriptedProvider.calls = 0
    monkeypatch.setattr(adventures, "OpenAICompatibleProvider", ScriptedProvider)
    monkeypatch.setattr(
        auth,
        "resolve_provider_config",
        lambda s: auth.ProviderConfig("http://fake", "k", "test-model", False),
    )
    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    c.adv_id = adv_id
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        adventures._active_turns.clear()
        Base.metadata.drop_all(bind=engine)


# ------------------------------------------------------------------ helpers

def _play(client, text="look around", after_id=None):
    body = {"type": "do", "text": text}
    if after_id is not None:
        body["after_id"] = after_id
    r = client.post(f"/api/adventures/{client.adv_id}/actions", json=body)
    assert r.status_code == 200, r.text
    return r


def _retry(client):
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text


def _branch_count(adv_id) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(models.Branch).filter(models.Branch.adventure_id == adv_id).count()
        )
    finally:
        db.close()


def _ai_rows(adv_id) -> list[models.Action]:
    """Every AI node ever written, oldest first — live or not, any branch."""
    db = SessionLocal()
    try:
        return (
            db.query(models.Action)
            .filter(
                models.Action.adventure_id == adv_id,
                models.Action.type == "ai",
            )
            .order_by(models.Action.id)
            .all()
        )
    finally:
        db.close()


def _group_size(action_id: int) -> int:
    db = SessionLocal()
    try:
        node = db.get(models.Action, action_id)
        return len(attempts.group(db, node))
    finally:
        db.close()


# ------------------------------------------------------------------ tests

def test_retaken_turn_groups_all_its_takes(client):
    """The baseline the rest of the file leans on: three takes, one turn."""
    _play(client)
    _retry(client)
    _retry(client)

    takes = _ai_rows(client.adv_id)
    assert len(takes) == 3
    for take in takes:
        assert _group_size(take.id) == 3, "every take sees the whole turn"


def test_a_forked_take_keeps_its_siblings(client):
    """The bug this subphase exists for.

    Under coordinate grouping the forked take reads 1/1: it has left the
    (branch, depth) the others are still at. The parent does not move with it.
    """
    _play(client)
    _retry(client)
    _retry(client)
    # The story moves past the turn, so taking a different take is now a fork.
    _play(client, "press on")

    first_take = _ai_rows(client.adv_id)[0]
    assert first_take.live is False

    # Writing below it is what forks -- see the next test.
    _play(client, "go back and try this instead", after_id=first_take.id)

    assert _group_size(first_take.id) == 3, (
        "a take forked onto its own branch is still a take of the same turn"
    )


def test_stepping_between_takes_creates_no_branch(client):
    """Looking is free. Only writing commits to a line."""
    _play(client)
    _retry(client)
    _play(client, "press on")
    before = _branch_count(client.adv_id)

    first_take = _ai_rows(client.adv_id)[0]
    # Reading a take: the pager fetches the turn's takes and shows one.
    r = client.get(
        f"/api/adventures/{client.adv_id}/actions/{first_take.id}/variants"
    )
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2

    assert _branch_count(client.adv_id) == before, "reading forked nothing"


def test_writing_below_a_passed_take_forks_exactly_once(client):
    _play(client)
    _retry(client)
    _play(client, "press on")
    before = _branch_count(client.adv_id)

    first_take = _ai_rows(client.adv_id)[0]
    _play(client, "a different way", after_id=first_take.id)

    assert _branch_count(client.adv_id) == before + 1, "one write, one branch"


def test_takes_under_one_parent_do_not_count_takes_under_its_sibling(client):
    """The player's own example: 3/3 on one line, 2/2 on the other.

    C1 and C2 are takes of the same turn. What is played *below* each of them
    is a different turn, and the two must not pool -- they share a depth, and
    until the fork they share a branch too.
    """
    _play(client)
    _retry(client)               # two takes at this turn: C1, C2 (C2 live)
    c1, c2 = _ai_rows(client.adv_id)[:2]

    # Below C2, the live one: a turn with three takes.
    _play(client, "down the second path")
    _retry(client)
    _retry(client)
    under_c2 = [a for a in _ai_rows(client.adv_id) if a.parent_id is not None]
    under_c2 = [a for a in under_c2 if a.id not in (c1.id, c2.id)]
    assert len(under_c2) == 3

    # Now take C1 instead, and play a turn with two takes below it.
    _play(client, "down the first path", after_id=c1.id)
    _retry(client)

    everything = _ai_rows(client.adv_id)
    under_c1 = [a for a in everything if a.parent_id not in (None,)
                and a.id not in (c1.id, c2.id) and a.id not in {x.id for x in under_c2}]
    assert len(under_c1) == 2

    assert _group_size(under_c2[0].id) == 3, "C2's line keeps its three takes"
    assert _group_size(under_c1[0].id) == 2, "C1's line counts only its own two"


def test_naming_a_take_that_is_already_the_story_just_plays_on(client):
    """`after_id` pointing at the tip is an ordinary turn, and forks nothing."""
    _play(client)
    before = _branch_count(client.adv_id)

    live = [a for a in _ai_rows(client.adv_id) if a.live][-1]
    _play(client, "carry on", after_id=live.id)

    assert _branch_count(client.adv_id) == before
