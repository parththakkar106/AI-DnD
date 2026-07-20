"""End-to-end HTTP tests for undo/retry state revert, driving real turns through
the actual routes + scripting engine with only the LLM provider mocked.

A script's output hook adds 10 gold each turn; we assert the shared scoreboard
behaves correctly across play / undo / retry.

    python -m pytest tests/test_turn_flow_integration.py -v
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

GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


class FakeProvider:
    """Stand-in for OpenAICompatibleProvider: streams one fixed line, no network."""
    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        yield ("text", "The torch flickers as you press onward.")


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="tester@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    adv = models.Adventure(user_id=user.id, title="Cave", script_state={})
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start", text="You enter a cave."))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold",
        output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    # Force a real (non-demo) turn that uses our fake provider.
    monkeypatch.setattr(adventures, "OpenAICompatibleProvider", FakeProvider)
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


def _state(adv_id):
    db = SessionLocal()
    try:
        return db.get(models.Adventure, adv_id).script_state
    finally:
        db.close()


def _play(client, type_="do", text="look around"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions", json={"type": type_, "text": text})
    assert r.status_code == 200, r.text
    return r


def test_play_then_undo_reverts_gold(client):
    assert _state(client.adv_id) == {}
    _play(client)
    assert _state(client.adv_id) == {"gold": 10}

    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 200, r.text
    assert _state(client.adv_id) == {}  # scoreboard rolled back


def test_two_turns_then_undo_reverts_only_last(client):
    _play(client)
    _play(client)
    assert _state(client.adv_id) == {"gold": 20}

    client.post(f"/api/adventures/{client.adv_id}/undo")
    assert _state(client.adv_id) == {"gold": 10}  # back to after turn 1, not 0


def test_retry_does_not_double_apply_gold(client):
    _play(client)
    assert _state(client.adv_id) == {"gold": 10}

    # Before the fix this produced 20 (output hook ran twice); now it stays 10.
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text
    assert _state(client.adv_id) == {"gold": 10}


def test_retry_then_undo_still_clean(client):
    _play(client)
    client.post(f"/api/adventures/{client.adv_id}/retry")
    assert _state(client.adv_id) == {"gold": 10}
    client.post(f"/api/adventures/{client.adv_id}/undo")
    assert _state(client.adv_id) == {}
