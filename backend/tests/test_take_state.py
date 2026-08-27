"""Phase 14 SP9: what a take does to the shared state.

A turn does not only write text. A script mutates `script_state`, and the
referee mutates `world_state`. Both are shared: they belong to the
adventure, not to the node. Playing a turn again must put them back to
where they were before that turn ran. Otherwise the new take stacks its
mutations on top of the one it replaces, and the numbers drift every time
the player asks for another take.

`retry` has provided this guarantee since SP4 (`attempts.roll_back_before`).
These tests confirm the same guarantee for the two roads SP9 opened: a take
of a turn the story moved past, and a take of the player's own turn. Both
create a branch, which matters because the rollback must survive leaving
the line it was on.

    python -m pytest tests/test_take_state.py -v
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

from app import auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import PromptParts
from app.routers import adventures

SCHEMA = {"player": {"hp": {"min": 0, "max": 100, "initial": 100}}}

# Ten gold a turn, every turn. A number that only ever increases makes a
# rollback failure obvious: if a take stacks instead of replacing, the gold
# total is off by exactly one turn's worth.
GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


class ScriptedProvider:
    last_usage = None
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
    user = models.User(is_guest=False, email="takestate@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="S", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, title="Vault", scenario_id=scenario.id,
        script_state={}, world_state={"player": {"hp": 100}},
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start", text="You begin."))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = [f"Take {n}." for n in range(1, 40)]
    ScriptedProvider.calls = 0
    monkeypatch.setattr(adventures, "OpenAICompatibleProvider", ScriptedProvider)
    monkeypatch.setattr(auth, "resolve_provider_config", lambda s: auth.ProviderConfig(
        "http://fake", "k", "test-model", False))
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


def _play(client, text="look around", after_id=None):
    body = {"type": "do", "text": text}
    if after_id is not None:
        body["after_id"] = after_id
    r = client.post(f"/api/adventures/{client.adv_id}/actions", json=body)
    assert r.status_code == 200, r.text


def _take(client, action_id, text=""):
    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{action_id}/takes",
        json={"text": text},
    )
    assert r.status_code == 200, r.text
    return r


def _gold(adv_id) -> int:
    db = SessionLocal()
    try:
        return (db.get(models.Adventure, adv_id).script_state or {}).get("gold", 0)
    finally:
        db.close()


def _rows(adv_id, type_):
    db = SessionLocal()
    try:
        return (
            db.query(models.Action)
            .filter(models.Action.adventure_id == adv_id, models.Action.type == type_)
            .order_by(models.Action.id)
            .all()
        )
    finally:
        db.close()


def test_the_script_runs_once_a_turn(client):
    """This test establishes the baseline the rest of the file depends on."""
    _play(client)
    assert _gold(client.adv_id) == 10
    _play(client, "press on")
    assert _gold(client.adv_id) == 20


def test_a_take_of_a_past_ai_turn_does_not_stack_its_script(client):
    """Two turns played, then the first one taken again.

    The take leaves the path just before turn one. The state it starts from
    is the state turn one started from: zero gold, not the twenty that two
    turns accumulated. The take's own run then adds ten.
    """
    _play(client)
    _play(client, "press on")
    assert _gold(client.adv_id) == 20

    first_ai = _rows(client.adv_id, "ai")[0]
    _take(client, first_ai.id)

    assert _gold(client.adv_id) == 10, "rolled back to before that turn, then run once"


def test_a_take_of_a_player_turn_does_not_stack_its_script(client):
    _play(client)
    _play(client, "press on")
    assert _gold(client.adv_id) == 20

    first_player = _rows(client.adv_id, "do")[0]
    _take(client, first_player.id, "> You do something else.")

    assert _gold(client.adv_id) == 10


def test_writing_below_a_passed_take_starts_from_that_take_s_state(client):
    """The `after_id` path, which forks while writing.

    The take being written under produced a state of ten gold, from its own
    turn. The turn played on top of it adds another ten. The twenty gold
    that the abandoned line reached has no effect on this branch.
    """
    _play(client)
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text
    _play(client, "press on")
    assert _gold(client.adv_id) == 20

    discarded = [a for a in _rows(client.adv_id, "ai") if not a.live][0]
    _play(client, "a different way", after_id=discarded.id)

    assert _gold(client.adv_id) == 20, "that take's ten, plus this turn's ten"


def test_the_line_left_behind_keeps_the_state_it_reached(client):
    """Switching back finds the abandoned line's numbers where it left them."""
    _play(client)
    _play(client, "press on")
    first_ai = _rows(client.adv_id, "ai")[0]
    _take(client, first_ai.id)
    assert _gold(client.adv_id) == 10

    branches = client.get(f"/api/adventures/{client.adv_id}/branches").json()
    root = [b for b in branches if b["parent_branch_id"] is None][0]
    r = client.post(f"/api/adventures/{client.adv_id}/branches/{root['id']}/switch")
    assert r.status_code == 200, r.text

    assert _gold(client.adv_id) == 20, "the first telling still has its two turns"
