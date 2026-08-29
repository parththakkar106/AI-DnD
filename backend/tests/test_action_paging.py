"""Opening an adventure fetches a window, not the whole story.

A story only ever gets longer. Production's longest is 607 actions and
589.5 kB in one response, and that number never decreases on its own. The
page load returns the newest `ACTION_PAGE` window, and the reader pages
upward from there.

The paging anchors on an action id rather than an offset, and these tests
cover why. An offset counted back from the newest shifts every older
position the moment a turn lands, which is exactly when a reader is likely
to be scrolling. An anchor means the same thing before and after.

    python -m pytest tests/test_action_paging.py -v
"""
import re

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers.adventures import ACTION_PAGE
from tools import dbmeter

TOTAL = ACTION_PAGE * 3 + 7  # deliberately not a whole number of pages


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="paging@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="m"))
    adventure = models.Adventure(user_id=user.id, title="Cave", script_state={})
    setup.add(adventure)
    setup.flush()
    for i in range(TOTAL):
        setup.add(models.Action(
            adventure_id=adventure.id,
            type="start" if i == 0 else ("ai" if i % 2 else "do"),
            text=f"Action {i}." + "word " * 200,
        ))
    setup.commit()
    adv_id, user_id = adventure.id, user.id
    setup.close()

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
        Base.metadata.drop_all(bind=engine)


def ordinal(action) -> int:
    """Returns which turn this is, read out of the fixture's own text.

    The payload used to carry `index`, a story-wide turn number that SP8
    dropped. Nothing replaced it: a depth is a position along one branch, and
    the pager keys on ids. The fixture numbers its own actions, so these tests
    read the number back rather than reintroduce one.
    """
    return int(re.match(r"Action (\d+)\.", action["text"]).group(1))


def page(client, before_id=None, limit=None):
    params = {}
    if before_id is not None:
        params["before_id"] = before_id
    if limit is not None:
        params["limit"] = limit
    r = client.get(f"/api/adventures/{client.adv_id}/actions", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def add_action(client, text="A new turn.") -> int:
    db = SessionLocal()
    try:
        action = models.Action(
            adventure_id=client.adv_id, type="ai", text=text
        )
        db.add(action)
        db.commit()
        return action.id
    finally:
        db.close()


# ------------------------------------------------------------- the page load

def test_the_page_load_returns_only_the_newest_window(client):
    r = client.get(f"/api/adventures/{client.adv_id}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["actions"]) == ACTION_PAGE
    assert body["action_count"] == TOTAL
    # It is the newest window, ending on the last action.
    assert ordinal(body["actions"][-1]) == TOTAL - 1
    assert ordinal(body["actions"][0]) == TOTAL - ACTION_PAGE


def test_a_short_story_is_returned_whole(client):
    db = SessionLocal()
    try:
        db.query(models.Action).filter(models.Action.depth >= 5).delete()
        db.commit()
    finally:
        db.close()

    body = client.get(f"/api/adventures/{client.adv_id}").json()
    assert len(body["actions"]) == 5
    assert body["action_count"] == 5


def test_the_page_load_does_not_grow_with_the_story(client):
    """Confirm that opening a story costs a window, regardless of the
    story's length."""
    meter = dbmeter.Meter()
    meter.attach(engine)
    try:
        with meter.scope("page load"):
            client.get(f"/api/adventures/{client.adv_id}")
        windowed = meter.scopes[-1].total.fetched
    finally:
        meter.detach()

    # Each action carries about 1 KB of text, and there are 187 of them. A
    # window holds 60 actions. This ceiling is generous but still far below
    # the size of the whole story.
    assert windowed < ACTION_PAGE * 2_000, f"{windowed:,} B for one window"
    assert windowed < TOTAL * 500, (
        f"{windowed:,} B — that is the whole story, not a window"
    )


# ------------------------------------------------------------------ paging up

def test_the_first_page_is_the_newest(client):
    body = page(client)
    assert len(body["actions"]) == ACTION_PAGE
    assert body["total"] == TOTAL
    assert body["has_more"] is True
    assert ordinal(body["actions"][-1]) == TOTAL - 1


def test_paging_up_covers_the_whole_story_exactly_once(client):
    seen = []
    body = page(client)
    seen = [ordinal(a) for a in body["actions"]]
    guard = 0
    while body["has_more"]:
        guard += 1
        assert guard < 20, "paging did not terminate"
        body = page(client, before_id=body["actions"][0]["id"])
        seen = [ordinal(a) for a in body["actions"]] + seen

    assert seen == list(range(TOTAL)), "gap, duplicate or reordering while paging"


def test_has_more_is_false_at_the_beginning_of_the_story(client):
    body = page(client)
    while body["has_more"]:
        body = page(client, before_id=body["actions"][0]["id"])
    assert ordinal(body["actions"][0]) == 0


def test_each_page_is_ordered_oldest_first(client):
    body = page(client)
    indices = [ordinal(a) for a in body["actions"]]
    assert indices == sorted(indices)


# ------------------------------------------------- the reason for the anchor

def test_a_turn_arriving_mid_scroll_does_not_shift_the_next_page(client):
    """Reproduce the failure an offset-based scheme would have. Read the
    newest page, let a turn land, then page up. The reader must get exactly
    what precedes the actions they already hold, with no duplicate and no
    skipped action."""
    first = page(client)
    oldest_held = first["actions"][0]

    add_action(client)

    older = page(client, before_id=oldest_held["id"])
    assert ordinal(older["actions"][-1]) == ordinal(oldest_held) - 1, (
        "the page shifted when a turn landed"
    )
    assert all(ordinal(a) < ordinal(oldest_held) for a in older["actions"])
    # The new turn changes the total, which is expected. It must not move the window.
    assert older["total"] == TOTAL + 1


def test_a_deleted_anchor_reports_the_end_rather_than_a_duplicate_page(client):
    """Undo can remove the action a slow scroll was anchored to. The endpoint
    must stop instead of returning a page the reader already has."""
    body = page(client)
    anchor = body["actions"][0]

    db = SessionLocal()
    try:
        db.query(models.Action).filter(models.Action.id == anchor["id"]).delete()
        db.commit()
    finally:
        db.close()

    after = page(client, before_id=anchor["id"])
    assert after["actions"] == []
    assert after["has_more"] is False


# ------------------------------------------------------------------- limits

def test_limit_is_honoured_and_capped(client):
    assert len(page(client, limit=5)["actions"]) == 5
    # A client that asks for the whole story cannot bypass the paging cap.
    assert len(page(client, limit=100_000)["actions"]) <= ACTION_PAGE * 4


def test_a_nonsense_limit_still_returns_something(client):
    assert len(page(client, limit=0)["actions"]) >= 1
    assert len(page(client, limit=-5)["actions"]) >= 1


# --------------------------------------------------------------------- undo

def test_undo_returns_a_window_not_the_story(client):
    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["actions"]) == ACTION_PAGE
    assert body["total"] == TOTAL - 1
    assert body["has_more"] is True
