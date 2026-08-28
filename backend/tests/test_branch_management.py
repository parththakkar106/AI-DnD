"""Phase 14 SP7: renaming a branch and deleting one.

SP5 gave the tree a fork and a switch. Neither operation removes anything,
and nothing in the design prunes a tree automatically, so an adventure that
is retried and forked enough grows without a limit. Delete is what limits
that growth. It ships with the same view that first makes a fork reachable,
rather than in a later subphase.

Two rules carry most of this file:

* A name is chosen, so the database stores it. A label is derived, so the
  database does not. An unnamed branch keeps NULL, and the client draws the
  label from its fork depth. A generated label such as "branch 4" stored in
  the column would become wrong the moment branch 3 is deleted.
* Delete must never remove a branch the reader depends on. Refusing to
  delete the head is the obvious case. Refusing to delete an ancestor of the
  head is the same rule applied one level up: deleting it would leave
  `head_branch_id` pointing at a row the cascade removed.

    python -m pytest tests/test_branch_management.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, limits, models, schemas
from app.context import cursors, lineage
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers import adventures

from fakes import ScriptedProvider


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="branches@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    adv = models.Adventure(
        user_id=user.id, title="Cave", script_state={}, world_state={},
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(
        adventure_id=adv.id, index=0, type="start", text="You enter a cave."))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = ["Attempt one.", "Attempt two.", "Next turn."]
    ScriptedProvider.calls = 0
    monkeypatch.setattr(adventures.turns, "OpenAICompatibleProvider", ScriptedProvider)
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
        adventures.turns._active_turns.clear()
        Base.metadata.drop_all(bind=engine)


# ------------------------------------------------------------------ helpers

def _play(client, text="look around"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": "do", "text": text})
    assert r.status_code == 200, r.text


def _retry(client):
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text


def _branches(client) -> list[dict]:
    r = client.get(f"/api/adventures/{client.adv_id}/branches")
    assert r.status_code == 200, r.text
    return r.json()


def _rename(client, branch_id, name):
    return client.patch(f"/api/adventures/{client.adv_id}/branches/{branch_id}",
                        json={"name": name})


def _delete(client, branch_id):
    return client.delete(f"/api/adventures/{client.adv_id}/branches/{branch_id}")


def _switch(client, branch_id):
    return client.post(f"/api/adventures/{client.adv_id}/branches/{branch_id}/switch")


def _texts(client) -> list[str]:
    return [
        a["text"]
        for a in client.get(f"/api/adventures/{client.adv_id}").json()["actions"]
    ]


def _discarded_on(adv_id, branch_id=None) -> int:
    """An AI attempt with no turn built on it, optionally restricted to one branch."""
    db = SessionLocal()
    try:
        q = db.query(models.Action).filter(
            models.Action.adventure_id == adv_id,
            models.Action.type == "ai",
            models.Action.live.is_(False),
        )
        if branch_id is not None:
            q = q.filter(models.Action.branch_id == branch_id)
        return q.order_by(models.Action.id).first().id
    finally:
        db.close()


def _forked(client):
    """A story with one fork. Returns (root id, forked id). The fork is head.

        start · do · [attempt one | ATTEMPT TWO] · do · next turn
                          └── forked here
    """
    _play(client)
    _retry(client)
    _play(client, "go deeper")
    root = _branches(client)[0]["id"]
    r = client.post(
        f"/api/adventures/{client.adv_id}/actions/{_discarded_on(client.adv_id)}/fork")
    assert r.status_code == 200, r.text
    forked = [b for b in _branches(client) if b["id"] != root][0]["id"]
    return root, forked


def _counts(adv_id, branch_ids):
    db = SessionLocal()
    try:
        return (
            db.query(models.Action)
            .filter(models.Action.branch_id.in_(branch_ids)).count(),
            db.query(models.Memory)
            .filter(models.Memory.branch_id.in_(branch_ids)).count(),
        )
    finally:
        db.close()


# ------------------------------------------------------------------ naming

def test_a_branch_starts_unnamed(client):
    """NULL, not a generated label. The client draws a label from the fork depth.

    A name stored here would go stale the moment a branch before it is
    deleted and the ordinals shift.
    """
    root, forked = _forked(client)
    assert [b["name"] for b in _branches(client)] == [None, None]


def test_a_name_is_stored_and_read_back(client):
    root, forked = _forked(client)
    r = _rename(client, forked, "the cellar")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "the cellar"
    assert {b["id"]: b["name"] for b in _branches(client)} == {
        root: None, forked: "the cellar",
    }


def test_a_blank_name_goes_back_to_unnamed(client):
    """A name of spaces is not a name anyone chose.

    Storing it would give the client an empty label instead of falling back
    to the fork depth. The branch would look nameless and appear broken.
    """
    root, forked = _forked(client)
    _rename(client, forked, "briefly named")
    assert _rename(client, forked, "   ").json()["name"] is None
    assert _rename(client, forked, None).json()["name"] is None


def test_a_name_longer_than_the_column_is_refused(client):
    """422 here rather than a 500 at INSERT: Postgres enforces VARCHAR(80)."""
    root, forked = _forked(client)
    assert _rename(client, forked, "x" * (schemas.BRANCH_NAME_MAX + 1)).status_code == 422
    assert _rename(client, forked, "x" * schemas.BRANCH_NAME_MAX).status_code == 200


def test_a_rename_hands_back_the_row_the_listing_would_give(client):
    """Renaming a branch does not change how many turns are on it.

    `own_actions` was hard-coded to 0 in this response. That bug stayed
    invisible because the panel discards the response body and refetches.
    Anything that trusted the reply would show a branch that had just lost
    its turns.
    """
    root, forked = _forked(client)
    listed = {b["id"]: b for b in _branches(client)}

    renamed = _rename(client, forked, "the cellar").json()

    assert renamed["own_actions"] == listed[forked]["own_actions"]
    assert renamed["own_actions"] > 0, "the fixture put turns on this branch"
    assert renamed["depth"] == listed[forked]["depth"]
    assert renamed["is_head"] == listed[forked]["is_head"]


def test_naming_a_branch_of_another_adventure_is_a_404(client):
    root, forked = _forked(client)
    db = SessionLocal()
    try:
        other = models.Adventure(
            user_id=db.get(models.Adventure, client.adv_id).user_id,
            title="Elsewhere", script_state={}, world_state={},
        )
        db.add(other)
        db.commit()
        other_id = other.id
    finally:
        db.close()
    r = client.patch(f"/api/adventures/{other_id}/branches/{forked}", json={"name": "x"})
    assert r.status_code == 404


# ----------------------------------------------------------------- deleting

def test_the_root_branch_cannot_be_deleted(client):
    """It holds the turns every other branch borrows."""
    root, forked = _forked(client)
    r = _delete(client, root)
    assert r.status_code == 400
    assert "adventure" in r.json()["detail"].lower()
    assert len(_branches(client)) == 2


def test_the_branch_being_read_cannot_be_deleted(client):
    root, forked = _forked(client)
    assert [b["is_head"] for b in _branches(client) if b["id"] == forked] == [True]
    r = _delete(client, forked)
    assert r.status_code == 400
    assert "switch" in r.json()["detail"].lower()


def test_an_ancestor_of_the_branch_being_read_cannot_be_deleted(client):
    """Deleting an ancestor of the head is the same mistake as deleting the head.

    `parent_branch_id` cascades, so deleting a branch the head was forked
    from would delete the head too, and leave `head_branch_id` pointing at
    nothing.
    """
    root, forked = _forked(client)
    # A fork of the fork, so `forked` is an ancestor of the head rather than
    # the head itself.
    _retry(client)
    _play(client, "press on")
    nested = _discarded_on(client.adv_id, branch_id=forked)
    r = client.post(f"/api/adventures/{client.adv_id}/actions/{nested}/fork")
    assert r.status_code == 200, r.text
    assert len(_branches(client)) == 3

    r = _delete(client, forked)
    assert r.status_code == 400
    assert "forked from it" in r.json()["detail"]
    assert len(_branches(client)) == 3


def test_deleting_a_branch_leaves_the_line_it_forked_from_untouched(client):
    root, forked = _forked(client)
    _switch(client, root)
    kept = _texts(client)

    assert _delete(client, forked).status_code == 204
    assert [b["id"] for b in _branches(client)] == [root]
    assert _texts(client) == kept, "the parent keeps every turn it had"


def test_deleting_a_branch_takes_its_nodes_and_its_descendants(client):
    """One statement deletes the whole subtree, regardless of depth. The
    cascade performs the traversal."""
    root, forked = _forked(client)
    _retry(client)
    _play(client, "press on")
    nested_attempt = _discarded_on(client.adv_id, branch_id=forked)
    client.post(f"/api/adventures/{client.adv_id}/actions/{nested_attempt}/fork")
    nested = [b["id"] for b in _branches(client) if b["id"] not in (root, forked)][0]

    doomed_actions, _ = _counts(client.adv_id, [forked, nested])
    assert doomed_actions > 0
    root_actions_before, _ = _counts(client.adv_id, [root])

    _switch(client, root)
    assert _delete(client, forked).status_code == 204

    assert [b["id"] for b in _branches(client)] == [root]
    assert _counts(client.adv_id, [forked, nested]) == (0, 0)
    assert _counts(client.adv_id, [root])[0] == root_actions_before


def test_deleting_a_branch_clears_a_cursor_that_stood_on_it(client):
    """Harmless on Postgres, a real bug on SQLite.

    Postgres never reuses a branch id, so a stale anchor simply never
    resolves. SQLite assigns the freed id to the next fork. The anchor then
    resolves onto a branch it never saw, and reports a stretch of story as
    already summarized. That stretch is then permanently excluded from the
    memories.
    """
    root, forked = _forked(client)
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        cursors.MEMORY.anchor(adventure, forked, 3)
        cursors.SUMMARY.anchor(adventure, root, 1)
        db.commit()
    finally:
        db.close()

    _switch(client, root)
    assert _delete(client, forked).status_code == 204

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        assert cursors.MEMORY.stored(adventure) == (None, cursors.NO_DEPTH)
        # The cursor on a branch that still exists is left exactly where it was.
        assert cursors.SUMMARY.stored(adventure) == (root, 1)
    finally:
        db.close()


def test_deleting_an_unknown_branch_is_a_404(client):
    root, forked = _forked(client)
    assert _delete(client, forked + 9999).status_code == 404


# --------------------------------------------------------- the bank vs a path

def _memories(client) -> list[dict]:
    r = client.get(f"/api/adventures/{client.adv_id}/memories")
    assert r.status_code == 200, r.text
    return r.json()


def _add_memory(client, text):
    r = client.post(f"/api/adventures/{client.adv_id}/memories", json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_hand_written_memory_is_anchored_where_it_was_written(client):
    """It takes the head, so it is a memory *of a story* rather than of an
    adventure. A NULL depth is a coordinate no fork can cap."""
    root, forked = _forked(client)
    memory_id = _add_memory(client, "Took the other door.")

    db = SessionLocal()
    try:
        memory = db.get(models.Memory, memory_id)
        adventure = db.get(models.Adventure, client.adv_id)
        assert memory.branch_id == forked, "the branch being read"
        assert memory.depth is not None, "never NULL again"
        assert memory.depth == adventure.head_depth
    finally:
        db.close()


def test_the_drawer_shows_the_path_being_read_and_nothing_else(client):
    """The memories a reader can see match the memories the model can retrieve.

    An adventure-wide list would include memories from branches this story
    never took. Those memories are never retrieved, and a reader could not
    distinguish them from the ones actually in play.
    """
    root, forked = _forked(client)
    on_the_fork = _add_memory(client, "Took the other door.")
    _switch(client, root)
    on_the_root = _add_memory(client, "Went the long way instead.")

    assert {m["id"] for m in _memories(client)} == {on_the_root}, \
        "the fork's memory is not on this story"

    # Switching to the fork shows its own memory and the root's. A fork
    # borrows its ancestors up to the point where it diverged from them.
    # The relationship is asymmetric on purpose: the parent never took the
    # fork's path.
    _switch(client, forked)
    listed = {m["id"] for m in _memories(client)}
    assert on_the_fork in listed
    assert on_the_root not in listed, "written after the fork left this branch"


def test_the_drawer_and_retrieval_agree_on_what_is_visible(client):
    """One predicate decides visibility, so a memory can never be listed but
    unretrievable, or the reverse. Two separate definitions of "on this
    path" would eventually diverge."""
    root, forked = _forked(client)
    _add_memory(client, "Took the other door.")
    _switch(client, root)
    _add_memory(client, "Went the long way instead.")

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        retrievable = {
            row[0] for row in db.execute(
                select(models.Memory.id).where(
                    models.Memory.adventure_id == adventure.id,
                    lineage.path_of(db, adventure).clause(models.Memory),
                )
            )
        }
    finally:
        db.close()

    assert {m["id"] for m in _memories(client)} == retrievable


def test_deleting_a_branch_deletes_the_memories_written_on_it(client):
    """Deleting a branch removes its memories from the database, not just
    from view: the cascade deletes the row along with the branch. This
    keeps a hidden memory from becoming unreachable in a different way: a
    memory you cannot currently see is on a branch you can still switch to,
    and deleting that branch deletes the memory permanently."""
    root, forked = _forked(client)
    doomed = _add_memory(client, "Took the other door.")
    _switch(client, root)

    db = SessionLocal()
    try:
        assert db.get(models.Memory, doomed) is not None, "still on its own branch"
    finally:
        db.close()

    assert _delete(client, forked).status_code == 204

    db = SessionLocal()
    try:
        assert db.get(models.Memory, doomed) is None, "gone with the branch"
    finally:
        db.close()


# ------------------------------------------------------------------- backup

def test_a_bundle_carries_the_name_a_player_chose(client):
    """A name is a decision, so the export includes it. This is the rule the
    v2 format is built on.

    `lineage` and the head depth stay out of the export because the
    importer can compute them from what the file already carries. A name
    is computed from nothing, so the export must carry it.
    """
    root, forked = _forked(client)
    _rename(client, root, "the long way")
    _rename(client, forked, "the cellar")

    exported = client.get(f"/api/adventures/{client.adv_id}/export").json()
    assert [b.get("name") for b in exported["branches"]] == ["the long way", "the cellar"]

    r = client.post("/api/adventures/import", json=exported)
    assert r.status_code == 201, r.text
    restored = client.get(f"/api/adventures/{r.json()['id']}/branches").json()
    assert [b["name"] for b in restored] == ["the long way", "the cellar"]


def test_an_unnamed_tree_exports_no_name_key(client):
    """Unchanged from the file SP6 wrote, for a tree nobody has named."""
    _forked(client)
    exported = client.get(f"/api/adventures/{client.adv_id}/export").json()
    assert all("name" not in b for b in exported["branches"])


def test_a_bundle_naming_a_branch_with_a_number_is_refused(client):
    """400 from the planner, not a database error three branches in."""
    _forked(client)
    exported = client.get(f"/api/adventures/{client.adv_id}/export").json()
    exported["branches"][1]["name"] = 7
    r = client.post("/api/adventures/import", json=exported)
    assert r.status_code == 400
    assert "not text" in r.json()["detail"]
