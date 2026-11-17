"""A context hook that edits the prompt must not merge the system and story
messages. Weak models lose the player's latest action when the whole prompt
arrives as one user message.

    python -m pytest tests/test_context_hook_split.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers import adventures

from fakes import ScriptedProvider, play_turn

# Adds a note at the end, as the seeded scenario scripts do.
APPEND = 'const modifier = (text) => ({ text: text + "\\n\\n[Note: dawn is near.]" });\nmodifier(text);'
# Replaces the whole prompt, so the start of the story is gone.
REPLACE = 'const modifier = (text) => ({ text: "Only this." });\nmodifier(text);'


def _client(monkeypatch, context_js):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="split@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    adv = models.Adventure(user_id=user.id, title="Cave", script_state={}, world_state={},
                           memory="The cave is old.")
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, type="start", text="You enter a cave."))
    setup.add(models.AdventureScript(adventure_id=adv.id, position=0, enabled=True,
                                     name="Note", context_js=context_js))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = ["The cave is dark."]
    monkeypatch.setattr(adventures.turns, "OpenAICompatibleProvider", ScriptedProvider)
    monkeypatch.setattr(auth, "resolve_provider_config", lambda s: auth.ProviderConfig(
        "http://fake", "k", "test-model", False))
    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)
    app.dependency_overrides[auth.get_current_user] = (
        lambda db=Depends(get_db): db.get(models.User, user_id))
    c = TestClient(app)
    c.adv_id = adv_id
    return c


@pytest.fixture()
def cleanup():
    yield
    app.dependency_overrides.clear()
    adventures.turns._active_turns.clear()
    Base.metadata.drop_all(bind=engine)


def test_appended_note_keeps_messages_apart(monkeypatch, cleanup):
    play_turn(_client(monkeypatch, APPEND), text="light a torch")
    system, story = ScriptedProvider.prompts[-1]
    assert "The cave is old." in system
    assert "light a torch" not in system
    assert story.startswith("You enter a cave.")
    assert "light a torch" in story and story.endswith("[Note: dawn is near.]")


def test_rewritten_story_start_falls_back_to_one_message(monkeypatch, cleanup):
    play_turn(_client(monkeypatch, REPLACE), text="light a torch")
    assert ScriptedProvider.prompts[-1] == ("", "Only this.")
