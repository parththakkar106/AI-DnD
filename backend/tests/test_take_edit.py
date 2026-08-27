"""Phase 14 SP9: editing the take you are actually reading.

The pager can display a turn on take 2 of 4. The transcript row for that
turn stays keyed by the live take, because that is the row the transcript
reports and the one the window carries. The take currently displayed is
known only to the client, by its own node id.

So "edit this" has two ids to choose from, and the page shipped with the
wrong one. It opened the editor on the live take's text and saved over it,
even from a row that was displaying take 2. This is a client bug, and it
is fixed in Play.jsx. The fix depends on a guarantee only the server can
make:

  A take is an ordinary row to the edit endpoint, addressed by its own id,
  whether or not it is the one the path runs through.

The fix also depends on the group listing reporting this correctly
afterward. This file asserts both guarantees, so the client's fix cannot
be silently broken.

    python -m pytest tests/test_take_edit.py -v
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
    user = models.User(is_guest=False, email="takeedit@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="S")
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, title="Vault", scenario_id=scenario.id,
        script_state={}, world_state={},
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start", text="You begin."))
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


def _play(client, text="look around"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": "do", "text": text})
    assert r.status_code == 200, r.text


def _retry(client):
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text


def _takes(client, action_id):
    r = client.get(f"/api/adventures/{client.adv_id}/actions/{action_id}/variants")
    assert r.status_code == 200, r.text
    return r.json()


def _edit(client, action_id, text):
    return client.patch(f"/api/adventures/{client.adv_id}/actions/{action_id}",
                        json={"text": text})


def _live_ai(client):
    """The AI row the transcript currently reports as live."""
    adv = client.get(f"/api/adventures/{client.adv_id}").json()
    return [a for a in adv["actions"] if a["type"] == "ai"][-1]


def _four_takes(client):
    """One turn, played four times. Returns (row the page holds, take list)."""
    _play(client)
    for _ in range(3):
        _retry(client)
    row = _live_ai(client)
    assert row["take_count"] == 4
    return row, _takes(client, row["id"])


def test_the_pager_reads_four_distinct_takes(client):
    """The premise: 2/4 and 4/4 are different rows with different words."""
    row, takes = _four_takes(client)
    assert [t["text"] for t in takes] == ["Take 1.", "Take 2.", "Take 3.", "Take 4."]
    assert takes[3]["id"] == row["id"], "the newest take is the one the story tells"
    assert takes[1]["id"] != row["id"], "2/4 is not the row the transcript is keyed by"


def test_editing_a_take_that_is_not_live_edits_that_take(client):
    """The bug, at the level the client's fix depends on.

    Saving against take 2's own id must land on take 2. The request must
    not be refused for being off the path, and must not be redirected onto
    the live row.
    """
    row, takes = _four_takes(client)
    second = takes[1]

    r = _edit(client, second["id"], "Take 2, rewritten.")
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "Take 2, rewritten."

    after = _takes(client, row["id"])
    assert [t["text"] for t in after] == [
        "Take 1.", "Take 2, rewritten.", "Take 3.", "Take 4.",
    ], "one take changed, and only the one addressed"


def test_editing_a_take_leaves_the_live_one_alone(client):
    """What the page did instead: the reader saw 2/4 and 4/4 was overwritten."""
    row, takes = _four_takes(client)

    _edit(client, takes[1]["id"], "Take 2, rewritten.")

    assert _live_ai(client)["text"] == "Take 4.", "the story still tells what it told"


def test_a_take_edit_survives_paging_away_and_back(client):
    """The listing is the pager's only source, so the edit must appear in it.

    The client caches this list per message, and the fix drops that cache
    after an edit. If the server ever started answering from a stale copy
    of its own, stepping away and back would show the text before the
    edit, and this test would not catch it. That is why this test makes
    the round trip.
    """
    row, takes = _four_takes(client)
    _edit(client, takes[1]["id"], "Take 2, rewritten.")

    # Addressed by the live row, as the pager does, and by the edited take
    # itself, as a client holding that id would.
    assert _takes(client, row["id"])[1]["text"] == "Take 2, rewritten."
    assert _takes(client, takes[1]["id"])[1]["text"] == "Take 2, rewritten."


def test_an_edited_take_is_still_the_take_it_was(client):
    """Editing text is not switching, forking, or reordering."""
    row, takes = _four_takes(client)
    _edit(client, takes[1]["id"], "Take 2, rewritten.")

    after = _takes(client, row["id"])
    assert [t["id"] for t in after] == [t["id"] for t in takes], "same rows, same order"
    assert [t["active"] for t in after] == [False, False, False, True]
    assert _live_ai(client)["take_index"] == 3, "still 4/4 on screen"
