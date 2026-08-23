"""HTTP tests for the AI Chat scratchpad (power users only).

Covers the access gate, the streamed reply, and the demo-key model pinning —
the part that must not let a public visitor reach paid models through this page.

    python -m pytest tests/test_chat.py -v
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
from app.routers import chat


class FakeProvider:
    """Records what it was constructed with, then streams a fixed reply. Stands
    in for the real egress point, so asserting on last_key/last_model is
    asserting on exactly what would have gone over the wire."""
    last_usage = None
    last_model = None
    last_key = None
    last_endpoint = None
    last_messages = None

    def __init__(self, endpoint_url, api_key, model, api_mode="chat", reasoning_max_tokens=0):
        FakeProvider.last_model = model
        FakeProvider.last_key = api_key
        FakeProvider.last_endpoint = endpoint_url

    async def chat(self, messages, *, temperature, max_tokens):
        FakeProvider.last_messages = messages
        yield ("reasoning", "hmm")
        yield ("text", "Hello back.")


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="power@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    setup.commit()
    user_id = user.id
    setup.close()

    monkeypatch.setattr(chat, "OpenAICompatibleProvider", FakeProvider)
    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    # Multi-user mode is what makes the power-user gate meaningful (local mode
    # trusts everyone); the allowlist is set per-test.
    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(auth, "POWER_USERS", {"power@example.com"})
    # These tests deliberately do NOT stub resolve_provider_config: the point is
    # to exercise the real BYOK-vs-demo decision, since that is what keeps the
    # shared key off paid models. Each test picks a mode with _byok/_demo below.

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def _send(client, **extra):
    return client.post("/api/chat/stream", json={"messages": [{"role": "user", "content": "hi"}], **extra})


def _byok(monkeypatch):
    """The user brought their own key: no demo key in play, any model allowed."""
    monkeypatch.setattr(auth, "demo_enabled", lambda: False)
    db = SessionLocal()
    try:
        settings = db.query(models.Settings).first()
        settings.api_key = "sk-my-own-key"  # legacy-plaintext path: used as-is
        db.commit()
    finally:
        db.close()


def _demo(monkeypatch, whitelist=("free/allowed",)):
    """The user has no key, so turns run on the server-funded demo key."""
    monkeypatch.setattr(auth, "demo_enabled", lambda: True)
    monkeypatch.setattr(auth, "DEMO_API_KEY", "demo-key")
    monkeypatch.setattr(auth, "DEMO_ENDPOINT_URL", "http://demo")
    monkeypatch.setattr(auth, "DEMO_MODELS", list(whitelist))


def test_non_power_user_gets_404(client, monkeypatch):
    monkeypatch.setattr(auth, "POWER_USERS", set())
    assert _send(client).status_code == 404
    assert client.get("/api/chat/config").status_code == 404


def test_power_user_streams_a_reply(client, monkeypatch):
    _byok(monkeypatch)
    resp = _send(client)
    assert resp.status_code == 200, resp.text
    assert '"type": "reasoning"' in resp.text
    assert "Hello back." in resp.text
    assert '"type": "done"' in resp.text
    assert FakeProvider.last_messages == [{"role": "user", "content": "hi"}]


def test_system_prompt_and_model_override_are_honoured(client, monkeypatch):
    _byok(monkeypatch)
    resp = client.post("/api/chat/stream", json={
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
        "model": "some/other-model",
    })
    assert resp.status_code == 200, resp.text
    # BYOK: any model the user names is passed straight through, on their key.
    assert FakeProvider.last_model == "some/other-model"
    assert FakeProvider.last_key == "sk-my-own-key"
    assert FakeProvider.last_messages[0] == {"role": "system", "content": "Be terse."}


def test_demo_key_pins_model_to_whitelist(client, monkeypatch):
    _demo(monkeypatch)
    resp = _send(client, model="expensive/paid-model")
    assert resp.status_code == 200, resp.text
    # Refused visibly: the whitelisted model runs instead, with a note. The
    # paid slug must never reach the wire alongside the server-funded key.
    assert FakeProvider.last_model == "free/allowed"
    assert FakeProvider.last_key == "demo-key"
    assert '"type": "note"' in resp.text

    # A whitelisted model is still selectable on the demo key.
    _demo(monkeypatch, ["free/allowed", "free/second"])
    resp = _send(client, model="free/second")
    assert resp.status_code == 200, resp.text
    assert FakeProvider.last_model == "free/second"


def test_demo_key_ignores_an_off_whitelist_settings_model(client, monkeypatch):
    """The override isn't the only untrusted input — Settings.model is user-set
    too, and it must be pinned the same way when there's no BYOK key."""
    _demo(monkeypatch)
    db = SessionLocal()
    try:
        db.query(models.Settings).first().model = "expensive/paid-model"
        db.commit()
    finally:
        db.close()
    resp = _send(client)
    assert resp.status_code == 200, resp.text
    assert FakeProvider.last_model == "free/allowed"


def test_demo_key_endpoint_cannot_be_redirected(client, monkeypatch):
    """A user-controlled endpoint_url would leak the key itself, which is worse
    than spending it — the demo branch pins the URL too."""
    _demo(monkeypatch)
    db = SessionLocal()
    try:
        db.query(models.Settings).first().endpoint_url = "http://attacker.example/v1"
        db.commit()
    finally:
        db.close()
    assert _send(client).status_code == 200
    assert FakeProvider.last_endpoint == "http://demo"
    assert FakeProvider.last_key == "demo-key"


def test_provider_config_refuses_server_funded_paid_model(monkeypatch):
    """The structural backstop: a hand-built config (a future code path that
    forgets to go through resolve_provider_config) can't run a server-funded
    turn on an off-whitelist model."""
    monkeypatch.setattr(auth, "DEMO_API_KEY", "demo-key")
    monkeypatch.setattr(auth, "DEMO_MODELS", ["free/allowed"])
    with pytest.raises(ValueError):
        auth.ProviderConfig("http://demo", "demo-key", "expensive/paid-model", True)
    auth.ProviderConfig("http://demo", "demo-key", "free/allowed", True)  # whitelisted: fine
    # The user's own key with any model stays fine.
    auth.ProviderConfig("http://any", "sk-mine", "expensive/paid-model", False)


def test_byok_user_may_reuse_the_demo_keys_value(client, monkeypatch):
    """Regression: the demo key is just an OpenRouter key, so a user can paste
    that same value into their own Settings. That's BYOK — they're paying — and
    it must not trip the guard. It used to raise on every resolution, which
    500'd GET /auth/me and took the whole SPA down (no nav, no chat)."""
    monkeypatch.setattr(auth, "demo_enabled", lambda: True)
    monkeypatch.setattr(auth, "DEMO_API_KEY", "shared-key")
    monkeypatch.setattr(auth, "DEMO_ENDPOINT_URL", "http://demo")
    monkeypatch.setattr(auth, "DEMO_MODELS", ["free/allowed"])
    db = SessionLocal()
    try:
        settings = db.query(models.Settings).first()
        settings.api_key = "shared-key"          # same value, but supplied by the user
        settings.model = "expensive/paid-model"  # their spend, their choice
        db.commit()
    finally:
        db.close()

    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/chat/config").status_code == 200
    resp = _send(client)
    assert resp.status_code == 200, resp.text
    assert FakeProvider.last_model == "expensive/paid-model"
    assert FakeProvider.last_key == "shared-key"


def test_resolve_provider_config_is_the_single_choke_point(monkeypatch):
    """Turns, AI Chat and the connection test all resolve through this one
    function, so pinning it here pins every caller. No DB or HTTP needed."""
    monkeypatch.setattr(auth, "demo_enabled", lambda: True)
    monkeypatch.setattr(auth, "DEMO_API_KEY", "demo-key")
    monkeypatch.setattr(auth, "DEMO_ENDPOINT_URL", "http://demo")
    monkeypatch.setattr(auth, "DEMO_MODELS", ["free/allowed"])

    # No key of their own: endpoint AND model are pinned, whatever they set.
    no_key = models.Settings(endpoint_url="http://mine/v1", api_key="", model="expensive/paid")
    assert auth.resolve_provider_config(no_key) == auth.ProviderConfig(
        "http://demo", "demo-key", "free/allowed", True)
    assert auth.resolve_provider_config(
        no_key, model_override="expensive/paid").model == "free/allowed"
    assert auth.resolve_provider_config(
        no_key, model_override="free/allowed").model == "free/allowed"

    # Their own key: their endpoint, their key, their choice of model.
    byok = models.Settings(endpoint_url="http://mine/v1", api_key="sk-mine", model="expensive/paid")
    assert auth.resolve_provider_config(byok) == auth.ProviderConfig(
        "http://mine/v1", "sk-mine", "expensive/paid", False)


def test_oversized_conversation_is_refused(client, monkeypatch):
    _byok(monkeypatch)
    huge = "x" * 90_000
    resp = client.post("/api/chat/stream", json={
        "messages": [{"role": "user", "content": huge} for _ in range(5)],
    })
    assert resp.status_code == 413, resp.text
