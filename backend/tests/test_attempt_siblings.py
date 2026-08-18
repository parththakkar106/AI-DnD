"""Phase 14 SP4 — a retry writes a sibling node instead of rewriting a row.

`test_retry_variants.py` is the behavioural contract, unchanged since before
the tree, and it still passes: the same URLs, the same payload shape, the same
outcomes. This file asserts the things that are *only* true of the new storage
— that a turn can be several rows, that exactly one of them is the story, and
that the arrangement costs neither an extra prompt nor an extra turn.

    python -m pytest tests/test_attempt_siblings.py -v
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
from sqlalchemy.orm import undefer

from app import attempts, auth, limits, models, tree
from app.context import cursors, history
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import PromptParts
from app.routers import adventures

SCHEMA = {"player": {"hp": {"min": 0, "max": 100, "initial": 100}}}

GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


class ScriptedProvider:
    replies: list = []
    calls = 0
    prompts: list = []

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        index = min(ScriptedProvider.calls, len(ScriptedProvider.replies) - 1)
        ScriptedProvider.calls += 1
        ScriptedProvider.prompts.append((parts.system, parts.story))
        yield ("text", ScriptedProvider.replies[index])


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="siblings@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="S", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, title="Cave", scenario_id=scenario.id,
        script_state={}, world_state={"player": {"hp": 100}},
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start", text="You enter a cave."))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = ["Attempt one."]
    ScriptedProvider.calls = 0
    ScriptedProvider.prompts = []
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


def _play(client, text="look around", type="do"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": type, "text": text})
    assert r.status_code == 200, r.text


def _retry(client):
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text


def _page(client) -> dict:
    return client.get(f"/api/adventures/{client.adv_id}").json()


def _rows(adv_id) -> list[models.Action]:
    """Every action row of the adventure, story or not, live or not.

    Undeferred, because the session is closed before the caller looks: the
    columns this file is about are exactly the ones a page load never loads.
    """
    db = SessionLocal()
    try:
        return (
            db.query(models.Action)
            .filter(models.Action.adventure_id == adv_id)
            .options(
                undefer(models.Action.state_after),
                undefer(models.Action.world_state_after),
                undefer(models.Action.context_snapshot),
            )
            .order_by(models.Action.depth, models.Action.variant_index)
            .all()
        )
    finally:
        db.close()


# ------------------------------------------------------------- the sibling

def test_a_retry_writes_a_second_row_at_the_same_coordinate(client):
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    _retry(client)

    rows = _rows(client.adv_id)
    ai = [a for a in rows if a.type == "ai"]
    assert len(ai) == 2, "a retry is a node, not a rewrite"
    assert {(a.branch_id, a.depth) for a in ai} == {(ai[0].branch_id, ai[0].depth)}
    assert [a.text for a in ai] == ["Attempt one.", "Attempt two."]
    # Exactly one of them is the story, and it is the newer take.
    assert [a.live for a in ai] == [False, True]
    # ...and the discarded attempt is untouched, not a copy of anything.
    assert ai[0].state_after is not None


def test_the_story_shows_and_counts_the_turn_once(client):
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    before = _page(client)["action_count"]
    _retry(client)
    after = _page(client)

    assert after["action_count"] == before, "a discarded attempt is not a turn"
    assert [a["type"] for a in after["actions"]] == ["start", "do", "ai"]
    assert after["actions"][-1]["text"] == "Attempt two."


def test_a_discarded_attempt_never_reaches_the_prompt(client):
    """The trap the branch clause exists to close, at sibling scale: the losing
    attempt sits at the same branch and depth as the live one, so anything
    reading the story by coordinate alone would replay both."""
    ScriptedProvider.replies = ["Attempt one.", "Attempt two.", "Next turn."]
    _play(client)
    _retry(client)
    _play(client, "go deeper")

    story = ScriptedProvider.prompts[-1][1]
    assert "Attempt two." in story
    assert "Attempt one." not in story


def test_switching_moves_the_story_onto_the_other_row(client):
    ScriptedProvider.replies = [
        "A scratch.\n```state\n{\"player.hp\": -5}\n```",
        "A beating.\n```state\n{\"player.hp\": -40}\n```",
    ]
    _play(client)
    _retry(client)
    newest_id = _page(client)["actions"][-1]["id"]

    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{newest_id}/variant", json={"index": 0})
    assert r.status_code == 200, r.text
    # A different row answers — that is the whole change.
    assert r.json()["id"] != newest_id
    assert r.json()["text"].startswith("A scratch")

    rows = _rows(client.adv_id)
    ai = [a for a in rows if a.type == "ai"]
    assert [a.live for a in ai] == [True, False]
    # Both takes are still there, byte for byte.
    assert [a.text.split(".")[0] for a in ai] == ["A scratch", "A beating"]


def test_the_assembled_prompt_is_stored_once_per_turn(client):
    """A snapshot is ~160 kB of prompt every attempt at a turn shares. Giving
    each sibling a copy would have made retry a permanent multiplier on the
    biggest column in the database, so the prompt moves with the live flag."""
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    _retry(client)

    def holders():
        return [
            a.id for a in _rows(client.adv_id)
            if a.type == "ai" and "sections" in (a.context_snapshot or {})
        ]

    live_holder = holders()
    assert len(live_holder) == 1
    newest = _page(client)["actions"][-1]
    assert live_holder == [newest["id"]]

    client.post(f"/api/adventures/{client.adv_id}/actions/{newest['id']}/variant",
                json={"index": 0})
    moved = holders()
    assert len(moved) == 1 and moved != live_holder, "the prompt follows the story"


# ------------------------------------------------------- removing the turn

def test_undo_takes_every_attempt_with_it(client):
    ScriptedProvider.replies = ["One.", "Two.", "Three."]
    _play(client)
    _retry(client)
    _retry(client)
    assert len([a for a in _rows(client.adv_id) if a.type == "ai"]) == 3

    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 200, r.text
    assert [a.type for a in _rows(client.adv_id)] == ["start"]


def test_deleting_a_retried_turn_deletes_its_attempts(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    newest = _page(client)["actions"][-1]

    r = client.delete(f"/api/adventures/{client.adv_id}/actions/{newest['id']}")
    assert r.status_code == 204, r.text
    assert [a.type for a in _rows(client.adv_id)] == ["start", "do"]


def test_deleting_a_turn_through_a_discarded_attempt_still_takes_the_turn(client):
    """The pager hands out whichever id it last saw, and a switch changes which
    row that is. Deleting through the losing sibling must not leave the story
    holding a turn with no attempts."""
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    discarded = [a for a in _rows(client.adv_id) if a.type == "ai" and not a.live][0]

    r = client.delete(f"/api/adventures/{client.adv_id}/actions/{discarded.id}")
    assert r.status_code == 204, r.text
    assert [a.type for a in _rows(client.adv_id)] == ["start", "do"]


# ------------------------------------------- what the holdback used to cover

def test_retrying_withdraws_the_memory_the_turn_produced(client):
    """Why summarization no longer holds the newest action back.

    A memory covering the newest turn used to be unreachable-by-construction:
    the summarizer stopped one action short, because a retry rewrote the row
    under a mark that had already moved past it. Now the mark and the memory
    both name the node, and replacing what a node says withdraws them — the
    same repair undo and delete already made, so the holdback was the only
    thing left that a retry needed.
    """
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        newest = history.newest(adventure)
        memory = models.Memory(
            adventure_id=adventure.id, text="You looked around.",
            source_start=1, source_end=newest.depth,
        )
        tree.attach_memory(memory, newest)
        db.add(memory)
        cursors.MEMORY.anchor_at(adventure, newest)
        cursors.SUMMARY.anchor_at(adventure, newest)
        db.commit()
        covered_depth = newest.depth
    finally:
        db.close()

    _retry(client)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        assert db.query(models.Memory).count() == 0, "the withdrawn memory is gone"
        # ...and the ground it covered is handed back, so the block is summarized
        # again from where it began rather than silently skipped.
        assert cursors.MEMORY.depth(db, adventure) == 0
        assert cursors.SUMMARY.depth(db, adventure) == 0
        assert covered_depth > 0
    finally:
        db.close()


def test_a_memory_on_an_earlier_turn_survives_a_retry(client):
    """Only the coordinate whose text changed is withdrawn."""
    ScriptedProvider.replies = ["One.", "Two.", "Three."]
    _play(client)
    _play(client, "go deeper")

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        earlier = history.tail(adventure, 3)[0]
        memory = models.Memory(
            adventure_id=adventure.id, text="An earlier block.",
            source_start=0, source_end=earlier.depth,
        )
        tree.attach_memory(memory, earlier)
        db.add(memory)
        db.commit()
    finally:
        db.close()

    _retry(client)

    db = SessionLocal()
    try:
        assert [m.text for m in db.query(models.Memory).all()] == ["An earlier block."]
    finally:
        db.close()


# -------------------------------------------------------------- the group

def test_the_group_cache_is_renumbered_as_attempts_arrive(client):
    ScriptedProvider.replies = ["One.", "Two.", "Three."]
    _play(client)
    assert _page(client)["actions"][-1]["variant_count"] == 0  # never retried
    _retry(client)
    _retry(client)

    ai = [a for a in _rows(client.adv_id) if a.type == "ai"]
    assert [a.variant_index for a in ai] == [0, 1, 2]
    assert {a.variant_count for a in ai} == {3}


def test_attempts_module_agrees_with_the_endpoint(client):
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)
    newest = _page(client)["actions"][-1]

    listed = client.get(
        f"/api/adventures/{client.adv_id}/actions/{newest['id']}/variants").json()
    db = SessionLocal()
    try:
        node = db.get(models.Action, newest["id"])
        group = attempts.group(db, node)
        assert [a.text for a in group] == [v["text"] for v in listed]
        assert attempts.live_in(group).id == newest["id"]
    finally:
        db.close()


def test_export_carries_every_attempt_as_its_own_node(client):
    """SP6 changed the answer here, and the reason is the whole of that subphase.

    A v1 bundle had one entry per turn and folded the group back into a
    `variants` array, because the format had nowhere else to put a second take.
    A v2 bundle has coordinates, so an attempt is a node in the file exactly as
    it is a node in the database, and `live` says which one is the story.
    """
    ScriptedProvider.replies = ["One.", "Two."]
    _play(client)
    _retry(client)

    bundle = client.get(f"/api/adventures/{client.adv_id}/export").json()
    ai = [a for a in bundle["actions"] if a["type"] == "ai"]
    assert [(a["text"], a["live"]) for a in ai] == [("One.", False), ("Two.", True)]
    assert len({(a["branch"], a["depth"]) for a in ai}) == 1, "one turn, two takes"
    assert "variants" not in ai[0], "nothing writes the repeating group any more"

    # ...and importing it puts the group back exactly as it stood.
    imported = client.post("/api/adventures/import", json=bundle).json()["id"]
    rows = _rows(imported)
    ai_rows = [a for a in rows if a.type == "ai"]
    assert [(a.text, a.live) for a in ai_rows] == [("One.", False), ("Two.", True)]
    assert len({(a.branch_id, a.depth) for a in ai_rows}) == 1
