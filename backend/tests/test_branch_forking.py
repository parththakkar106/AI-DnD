"""Phase 14 SP5: continuing from a discarded attempt forks a branch.

SP4 made every attempt at a turn a node. While the attempts sit at the
tip, they are leaves and cost nothing: switching between them just moves
the `live` flag. The moment the player continues from an attempt the line
has already moved past, the two futures must coexist. That is a branch.

This file tests the claim the whole design rests on: a fork inserts one
row and moves one row, no matter how large the story behind it is.
Everything before the fork is borrowed, not copied, and the arithmetic
that makes borrowing possible lives in `lineage`.

    python -m pytest tests/test_branch_forking.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, limits, models
from app.context import cursors, lineage
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers import adventures

from fakes import ScriptedProvider

# `hp` moves freely. `mana` has a cooldown of 2 turns, so an incorrect
# advance shows up as a change the referee should have rejected.
SCHEMA = {
    "player": {
        "hp": {"min": 0, "max": 100, "initial": 100},
        "mana": {"min": 0, "max": 50, "initial": 50, "cooldown": 2},
    }
}

GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="fork@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="S", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, title="Cave", scenario_id=scenario.id,
        script_state={}, world_state={"player": {"hp": 100, "mana": 50}},
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

    ScriptedProvider.replies = ["A reply."]
    ScriptedProvider.calls = 0
    ScriptedProvider.prompts = []
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

def _play(client, text="look around", type="do"):
    r = client.post(f"/api/adventures/{client.adv_id}/actions",
                    json={"type": type, "text": text})
    assert r.status_code == 200, r.text


def _retry(client):
    r = client.post(f"/api/adventures/{client.adv_id}/retry")
    assert r.status_code == 200, r.text


def _texts(client) -> list[str]:
    return [a["text"] for a in client.get(f"/api/adventures/{client.adv_id}").json()["actions"]]


def _branches(client) -> list[dict]:
    r = client.get(f"/api/adventures/{client.adv_id}/branches")
    assert r.status_code == 200, r.text
    return r.json()


def _fork(client, action_id):
    return client.post(f"/api/adventures/{client.adv_id}/actions/{action_id}/fork")


def _state(adv_id):
    db = SessionLocal()
    try:
        adv = db.get(models.Adventure, adv_id)
        return adv.script_state, adv.world_state
    finally:
        db.close()


def _rows(adv_id) -> list[models.Action]:
    db = SessionLocal()
    try:
        return (
            db.query(models.Action)
            .filter(models.Action.adventure_id == adv_id)
            .order_by(models.Action.depth, models.Action.variant_index)
            .all()
        )
    finally:
        db.close()


def _divergent_story(client):
    """A story that retried turn 2, continued from the newer take, and left
    the older one behind as a leaf.

        start > do > [attempt one | ATTEMPT TWO] > do > next turn

    Returns the id of the discarded attempt.
    """
    ScriptedProvider.replies = ["Attempt one.", "Attempt two.", "Next turn."]
    _play(client)
    _retry(client)
    _play(client, "go deeper")
    return [a.id for a in _rows(client.adv_id) if a.type == "ai" and not a.live][0]


# ---------------------------------------------------------------- the fork

def test_a_fork_inserts_one_branch_row_and_copies_no_actions(client):
    discarded = _divergent_story(client)
    rows_before = [a.id for a in _rows(client.adv_id)]
    assert len(_branches(client)) == 1

    r = _fork(client, discarded)
    assert r.status_code == 200, r.text

    branches = _branches(client)
    assert len(branches) == 2, "exactly one branch row per divergence built on"
    assert [a.id for a in _rows(client.adv_id)] == rows_before, "a fork copies nothing"
    forked = [b for b in branches if b["parent_branch_id"] is not None][0]
    assert forked["is_head"] is True
    assert forked["own_actions"] == 1, "the promoted attempt, and nothing else"
    # The fork point is the depth just before the attempt. The code stores
    # this value instead of inferring it from where two branches first
    # differ, because that inference would guess wrong as soon as an
    # attempt repeats its parent's text.
    assert forked["fork_depth"] == forked["depth"] - 1


def test_the_lineage_is_capped_at_the_fork_depth(client):
    discarded = _divergent_story(client)
    _fork(client, discarded)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        branch = lineage.branch_of(db, adventure)
        entries = lineage.entries_of(branch)
        assert entries[0] == (branch.id, None), "its own nodes, to the tip"
        assert entries[1] == (branch.parent_branch_id, branch.fork_depth)
        assert len(entries) == 2
    finally:
        db.close()


def test_both_branches_read_independently(client):
    discarded = _divergent_story(client)
    parent = _branches(client)[0]["id"]
    _fork(client, discarded)

    # The fork's story: everything up to the divergence, then the other take.
    assert _texts(client) == [
        "You enter a cave.", "> You look around.", "Attempt one.",
    ]
    # The branch it left behind is unchanged, including turns after the fork.
    r = client.post(f"/api/adventures/{client.adv_id}/branches/{parent}/switch")
    assert r.status_code == 200, r.text
    assert _texts(client) == [
        "You enter a cave.", "> You look around.", "Attempt two.",
        "> You go deeper.", "Next turn.",
    ]


def test_the_parent_keeps_a_live_attempt_where_the_fork_left(client):
    """Promoting the other attempt must not leave the parent with a gap in
    its story. A coordinate with no live node is a turn that disappears
    from the read."""
    discarded = _divergent_story(client)
    _fork(client, discarded)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        parent_id = db.query(models.Branch).filter_by(
            adventure_id=adventure.id, parent_branch_id=None).one().id
        per_coordinate = {}
        for row in _rows(client.adv_id):
            per_coordinate.setdefault((row.branch_id, row.depth), []).append(row)
        for (branch_id, depth), group in per_coordinate.items():
            live = [a for a in group if a.live]
            assert len(live) == 1, f"branch {branch_id} depth {depth}"
        # The parent's turn 2 is now a single take, so the pager no longer
        # offers a page through attempts that diverged onto another branch.
        parent_turn = per_coordinate[(parent_id, 2)]
        assert len(parent_turn) == 1
        assert parent_turn[0].variant_count == 0
    finally:
        db.close()


def test_playing_on_a_fork_continues_that_branchs_depths(client):
    """A depth is a position along this story. Numbering the next node from
    the adventure-wide index would leave a gap where the other branch's
    turns are, and every windowing estimate would then have to work around
    that gap."""
    discarded = _divergent_story(client)
    _fork(client, discarded)
    ScriptedProvider.replies = ["Onward."]
    _play(client, "turn back")

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        path = lineage.path_of(db, adventure)
        rows = [a for a in _rows(client.adv_id) if path.contains(a)]
        assert [a.depth for a in sorted(rows, key=lambda a: a.depth)] == [0, 1, 2, 3, 4]
    finally:
        db.close()
    assert _texts(client)[-1] == "Onward."


# ------------------------------------------------------------- not a fork

def test_forking_at_the_tip_switches_without_making_a_branch(client):
    """Attempts nobody has built on stay leaves. This is what keeps the
    lineage a list of divergences instead of a list of every retry."""
    ScriptedProvider.replies = ["Attempt one.", "Attempt two."]
    _play(client)
    _retry(client)
    discarded = [a.id for a in _rows(client.adv_id) if a.type == "ai" and not a.live][0]

    r = _fork(client, discarded)
    assert r.status_code == 200, r.text
    assert len(_branches(client)) == 1, "no branch for an attempt at the tip"
    assert _texts(client)[-1] == "Attempt one."


def test_forking_the_attempt_already_in_the_story_does_nothing(client):
    """This call is idempotent. A client that has lost track of which take
    is live must not create a new branch on every click."""
    discarded = _divergent_story(client)
    _fork(client, discarded)
    promoted = [a.id for a in _rows(client.adv_id) if a.type == "ai" and a.live
                and a.branch_id != _branches(client)[0]["id"]][0]
    before = _texts(client)

    for _ in range(3):
        r = _fork(client, promoted)
        assert r.status_code == 200, r.text
    assert len(_branches(client)) == 2
    assert _texts(client) == before


def test_forking_a_turn_that_is_already_the_story_is_a_no_op(client):
    ScriptedProvider.replies = ["Only take."]
    _play(client)
    only = [a.id for a in _rows(client.adv_id) if a.type == "ai"][0]

    r = _fork(client, only)
    assert r.status_code == 200, r.text
    assert len(_branches(client)) == 1


def test_forking_a_live_node_on_another_branch_is_refused(client):
    """A live node off the path belongs to another branch's story, not to a
    spare attempt on this one. The refusal names the tool that actually
    switches branches. It used to answer "only one take", which was true
    of the attempt group but useless here: the caller does not want
    another take, it wants the branch this node is on."""
    discarded = _divergent_story(client)
    _fork(client, discarded)
    parent_id = [b for b in _branches(client) if b["parent_branch_id"] is None][0]["id"]
    stranded = [a.id for a in _rows(client.adv_id)
                if a.branch_id == parent_id and a.depth == 2][0]

    r = _fork(client, stranded)
    assert r.status_code == 400
    assert "another branch" in r.json()["detail"]
    # Refusing must leave the tree alone. The bug this guards against is a
    # fork that promotes a sibling on the branch it was called against.
    assert len(_branches(client)) == 2


# -------------------------------------------------------------- the state

def test_switching_restores_the_script_and_world_state(client):
    ScriptedProvider.replies = [
        "A scratch.\n```state\n{\"player.hp\": -5}\n```",
        "A beating.\n```state\n{\"player.hp\": -40}\n```",
        "Onward.",
    ]
    _play(client)
    _retry(client)
    _play(client, "go deeper")
    parent = _branches(client)[0]["id"]
    on_parent = _state(client.adv_id)

    discarded = [a.id for a in _rows(client.adv_id) if a.type == "ai" and not a.live][0]
    _fork(client, discarded)
    script_state, world_state = _state(client.adv_id)
    assert world_state["player"]["hp"] == 95, "the attempt this branch tells"
    assert script_state == {"gold": 10}, "one turn of gold, not three"

    client.post(f"/api/adventures/{client.adv_id}/branches/{parent}/switch")
    assert _state(client.adv_id) == on_parent


def test_the_cooldown_clock_travels_with_the_branch(client):
    """The world-state clock is a depth, and depths repeat across branches,
    so it can only be correct if each branch carries its own. It does,
    without extra work: the clock lives inside `_meta.last_changed`, which
    is part of the world state a switch restores."""
    ScriptedProvider.replies = [
        "Drained.\n```state\n{\"player.mana\": -10}\n```",
        "Untouched.",
        "Onward.",
    ]
    _play(client)
    _retry(client)
    _play(client, "go deeper")
    discarded = [a.id for a in _rows(client.adv_id) if a.type == "ai" and not a.live][0]
    on_parent = _state(client.adv_id)[1]
    assert on_parent["_meta"]["last_changed"].get("player.mana") is None

    _fork(client, discarded)
    forked = _state(client.adv_id)[1]
    assert forked["player"]["mana"] == 40
    assert forked["_meta"]["last_changed"]["player.mana"] == 2

    parent = [b for b in _branches(client) if b["parent_branch_id"] is None][0]["id"]
    client.post(f"/api/adventures/{client.adv_id}/branches/{parent}/switch")
    assert _state(client.adv_id)[1] == on_parent


def test_a_retry_does_not_advance_the_cooldown_clock(client):
    """SP5's one carried-over open item. A retry re-runs the same turn, so
    the clock the cooldown rules read must not move. The reused `index`
    used to guarantee this; the reused depth guarantees it now."""
    ScriptedProvider.replies = [
        "Drained.\n```state\n{\"player.mana\": -10}\n```",
        "Drained again.\n```state\n{\"player.mana\": -10}\n```",
    ]
    _play(client)
    first = _state(client.adv_id)[1]["_meta"]["last_changed"]["player.mana"]
    _retry(client)
    assert _state(client.adv_id)[1]["_meta"]["last_changed"]["player.mana"] == first
    # The second attempt's drain must land, instead of being rejected for a
    # cooldown it was never actually subject to.
    assert _state(client.adv_id)[1]["player"]["mana"] == 40


# --------------------------------------------------------- derived work

def test_a_memory_on_the_line_left_behind_is_out_of_range_on_the_fork(client):
    """Nothing is moved or removed when a branch forks. The memory attaches
    to the coordinate the parent's attempt still occupies, and the lineage
    caps the parent one depth short of it. The fork therefore cannot see
    this memory, and resummarizes that span from the text it actually
    contains."""
    from app import tree

    discarded = _divergent_story(client)
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        winner = db.query(models.Action).filter_by(
            adventure_id=adventure.id, type="ai", live=True).order_by(
            models.Action.depth).first()
        memory = models.Memory(
            adventure_id=adventure.id, text="Attempt two happened.",
            source_start=0, source_end=winner.depth,
        )
        tree.attach_memory(memory, winner)
        db.add(memory)
        cursors.MEMORY.anchor_at(adventure, winner)
        db.commit()
    finally:
        db.close()

    _fork(client, discarded)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        # The memory is still there, untouched, because it describes the
        # parent's story, which is unchanged.
        assert [m.text for m in db.query(models.Memory).all()] == ["Attempt two happened."]
        path = lineage.path_of(db, adventure)
        visible = db.query(models.Memory).filter(
            models.Memory.adventure_id == adventure.id,
            path.clause(models.Memory),
        ).all()
        assert visible == [], "a sibling's memory reached this branch"
        # The cursor reads one depth short of the memory, so this branch
        # treats the block as due again instead of silently marking it read.
        assert cursors.MEMORY.depth(db, adventure) == 1
    finally:
        db.close()


# ------------------------------------------------------------------- undo

def test_undo_stops_at_the_fork(client):
    """Undoing a turn on a fork must never reach into the branch it forked
    from. Those turns belong to that branch's story too."""
    discarded = _divergent_story(client)
    _fork(client, discarded)
    rows_before = len(_rows(client.adv_id))

    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 200, r.text
    # The promoted attempt is removed, and the player action before it
    # stays, because that action belongs to the parent and the parent
    # still has it.
    assert len(_rows(client.adv_id)) == rows_before - 1
    assert _texts(client) == ["You enter a cave.", "> You look around."]

    # Nothing is left of this branch's own turns, so undo must refuse
    # instead of removing the parent's turns.
    r = client.post(f"/api/adventures/{client.adv_id}/undo")
    assert r.status_code == 400
    assert "forked from" in r.json()["detail"]


# ----------------------------------------------------------- the tree view

def test_the_branch_list_is_the_tree(client):
    discarded = _divergent_story(client)
    _fork(client, discarded)
    ScriptedProvider.replies = ["Onward."]
    _play(client, "turn back")

    branches = _branches(client)
    root = [b for b in branches if b["parent_branch_id"] is None][0]
    forked = [b for b in branches if b["parent_branch_id"] == root["id"]][0]
    assert root["fork_depth"] is None and root["depth"] == 4
    assert forked["fork_depth"] == 1 and forked["depth"] == 4
    assert root["own_actions"] == 5 and forked["own_actions"] == 3
    assert forked["is_head"] is True and root["is_head"] is False


def test_fork_agrees_with_the_lineage_computed_by_hand(client):
    """`test_branch_clause.make_branch` has computed a fork's lineage by
    hand since SP2, precisely so the fixture could not pass by repeating a
    bug in the code under test. SP5 introduces that code, so this test
    checks the two against each other instead of letting them drift
    apart."""
    from tests.test_branch_clause import make_branch

    discarded = _divergent_story(client)
    _fork(client, discarded)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        real = lineage.branch_of(db, adventure)
        parent = db.get(models.Branch, real.parent_branch_id)
        by_hand = make_branch(db, adventure, parent=parent, fork_depth=real.fork_depth)
        # The two branches share a shape but not an id, so compare only the
        # ancestry, which is the arithmetic part rather than the allocated id.
        assert lineage.entries_of(real)[1:] == lineage.entries_of(by_hand)[1:]
        assert lineage.entries_of(real)[0] == (real.id, None)
    finally:
        db.close()


def test_a_deep_fork_chain_reads_for_what_one_branch_costs(client):
    """Clause count is bounded by the window, not by fork count. This is
    the property the whole lineage cache exists for, now measured through
    real forks instead of hand-built rows."""
    from tools import dbmeter

    ScriptedProvider.replies = ["First take.", "Second take.", "Onward."]
    _play(client)
    for _ in range(8):
        _retry(client)
        _play(client, "onward")
        discarded = [
            a.id for a in _rows(client.adv_id)
            if a.type == "ai" and not a.live
        ]
        if discarded:
            assert _fork(client, discarded[-1]).status_code == 200
            _play(client, "onward")
    branches = _branches(client)
    assert len(branches) > 4, "the fixture did not actually fork"

    meter = dbmeter.Meter()
    meter.attach(engine)
    try:
        with meter.scope("page"):
            client.get(f"/api/adventures/{client.adv_id}")
        page_bytes = meter.scopes[-1].total.fetched
    finally:
        meter.detach()
    assert page_bytes > 0, "the meter saw nothing"

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        entries = lineage.entries_of(lineage.branch_of(db, adventure))
        # The whole ancestry is available to be named.
        assert len(entries) == len(branches)
        # The windowed read names as few of them as the window needs.
        path = lineage.path_of(db, adventure)
        assert path.prefix_covering(60) <= len(entries)
    finally:
        db.close()


def test_switching_to_a_branch_of_another_adventure_is_a_404(client):
    """A branch id names one adventure, so the two ids in the URL must
    agree. Otherwise a guessed number reads somebody else's story."""
    other = client.post("/api/adventures", json={"title": "Elsewhere"}).json()["id"]
    ScriptedProvider.replies = ["Elsewhere."]
    r = client.post(f"/api/adventures/{other}/actions", json={"type": "do", "text": "wait"})
    assert r.status_code == 200, r.text
    stranger = client.get(f"/api/adventures/{other}/branches").json()
    assert len(stranger) == 1

    assert client.post(
        f"/api/adventures/{client.adv_id}/branches/{stranger[0]['id']}/switch"
    ).status_code == 404
