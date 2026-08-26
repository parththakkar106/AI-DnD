"""Phase 14 SP2: a read sees one story, and knows which one.

These tests build the fork by hand. Three branch rows and their nodes are
written straight to the database, arranged as the design doc's own worked
example. That was the only way to build one when this file was written,
because nothing forked until SP5. It stays that way now that `tree.fork`
exists, because a fixture built with the same code under test could not
catch that code being wrong. `test_branch_forking.py` checks the two
against each other.

    branch C, tip at depth 7, lineage [(C, 7), (B, 5), (A, 3)]
    -> A0 A1 A2 A3 B4 B5 C6 C7

The point of building the fixture by hand is that every read in the app
must go through one module. A forgotten clause does not raise an error. It
silently shows a story assembled out of two different branches. The
fixture deliberately leaves nodes where a forgotten clause would pick them
up: A kept playing past the fork (A4, A5), B kept playing past its own
(B6), and a second adventure holds a whole story of its own. None of these
nodes may appear on C.

    python -m pytest tests/test_branch_clause.py -v
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
from sqlalchemy import event

from app import auth, limits, models, tree
from app.context import history, lineage
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.scripting import ScriptPipeline
from tools import dbmeter


# --------------------------------------------------------------- the fixture

def make_branch(db, adventure, parent=None, fork_depth=None):
    """A branch row whose lineage is its parent's, capped, plus itself.

    This function performs the same computation SP5 does at fork time. It
    is written out here so the fixture cannot pass by repeating a bug in
    the code under test.
    """
    branch = models.Branch(
        adventure_id=adventure.id,
        parent_branch_id=parent.id if parent else None,
        fork_depth=fork_depth,
        lineage=[],
    )
    db.add(branch)
    db.flush()
    inherited = []
    if parent is not None:
        for ancestor_id, cap in lineage.entries_of(parent):
            capped = fork_depth if cap is None else min(cap, fork_depth)
            inherited.append([ancestor_id, capped])
    branch.lineage = [[branch.id, None]] + inherited
    db.flush()
    return branch


def add_node(db, adventure, branch, depth, label, index=None):
    action = models.Action(
        adventure_id=adventure.id,
        index=depth if index is None else index,
        branch_id=branch.id,
        depth=depth,
        type="start" if depth == 0 else ("ai" if depth % 2 else "do"),
        text=f"{label}{depth}",
    )
    db.add(action)
    return action


def make_adventure(db, user, title):
    adventure = models.Adventure(user_id=user.id, title=title, script_state={})
    db.add(adventure)
    db.flush()
    return adventure


@pytest.fixture()
def forked():
    """The worked example, plus everything a forgotten clause would expose."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="branch@example.com")
    db.add(user)
    db.flush()
    db.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="m"))

    adventure = make_adventure(db, user, "Forked")
    a = make_branch(db, adventure)
    b = make_branch(db, adventure, parent=a, fork_depth=3)
    c = make_branch(db, adventure, parent=b, fork_depth=5)

    for depth in range(4):
        add_node(db, adventure, a, depth, "A")
    # A did not stop when B forked off it: these two are siblings of B4/B5.
    for depth in (4, 5):
        add_node(db, adventure, a, depth, "A", index=100 + depth)
    for depth in (4, 5):
        add_node(db, adventure, b, depth, "B")
    add_node(db, adventure, b, 6, "B", index=200 + 6)  # B's own continuation
    for depth in (6, 7):
        add_node(db, adventure, c, depth, "C")

    # A second adventure, so "does the clause remember its adventure?" has an
    # answer. A database holding one adventure cannot tell you.
    other = make_adventure(db, user, "Elsewhere")
    other_branch = make_branch(db, other)
    for depth in range(4):
        add_node(db, other, other_branch, depth, "X")

    adventure.head_branch_id = c.id
    adventure.head_depth = 7
    other.head_branch_id = other_branch.id
    other.head_depth = 3
    db.commit()

    ids = {"a": a.id, "b": b.id, "c": c.id, "adventure": adventure.id,
           "other": other.id, "user": user.id}
    try:
        yield db, adventure, ids
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def switch_to(db, adventure, branch_id, tip):
    """Move the head, the way SP7's branch picker will."""
    adventure.head_branch_id = branch_id
    adventure.head_depth = tip
    db.commit()


def labels(actions):
    return [a.text for a in actions]


# ------------------------------------------------------------- the story read

def test_the_worked_example_reads_back_as_the_design_doc_says(forked):
    db, adventure, _ = forked
    assert labels(history.story_actions(adventure)) == [
        "A0", "A1", "A2", "A3", "B4", "B5", "C6", "C7"
    ]


def test_a_siblings_nodes_are_invisible(forked):
    db, adventure, _ = forked
    seen = labels(history.story_actions(adventure))
    # A4 and A5 are A's own continuation past B's fork. B6 is B's own
    # continuation past C's fork.
    assert "A4" not in seen and "A5" not in seen and "B6" not in seen
    # Nothing from the other adventure appears either.
    assert not [text for text in seen if text.startswith("X")]


def test_each_branch_reads_its_own_story(forked):
    db, adventure, ids = forked
    switch_to(db, adventure, ids["a"], 5)
    assert labels(history.story_actions(adventure)) == [
        "A0", "A1", "A2", "A3", "A4", "A5"
    ]
    switch_to(db, adventure, ids["b"], 6)
    assert labels(history.story_actions(adventure)) == [
        "A0", "A1", "A2", "A3", "B4", "B5", "B6"
    ]
    switch_to(db, adventure, ids["c"], 7)
    assert labels(history.story_actions(adventure)) == [
        "A0", "A1", "A2", "A3", "B4", "B5", "C6", "C7"
    ]


def test_the_ancestors_shared_nodes_are_shared_not_copied(forked):
    db, adventure, ids = forked
    # A0..A3 appear on all three stories and exist exactly once in the table.
    rows = (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adventure.id,
                models.Action.text == "A2")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].branch_id == ids["a"]


def test_count_and_tail_and_slice_agree_with_the_path(forked):
    db, adventure, _ = forked
    assert history.count(adventure) == 8
    assert labels(history.tail(adventure, 3)) == ["B5", "C6", "C7"]
    assert labels(history.tail_range(adventure, 2, 2)) == ["B4", "B5"]
    assert labels(history.slice_(adventure, 3, 3)) == ["A3", "B4", "B5"]


def test_a_window_that_reaches_past_the_fork_still_reads_in_order(forked):
    db, adventure, _ = forked
    # 32 is history.WINDOW_START: more than the whole story, so the read has to
    # widen through all three lineage entries and still come back in order.
    assert labels(history.window_covering(adventure, 10 ** 6, len)) == [
        "A0", "A1", "A2", "A3", "B4", "B5", "C6", "C7"
    ]


# ------------------------------------------- the loaded-collection short cut

def test_an_already_loaded_collection_is_cut_down_to_the_path(forked):
    """Tests `history._from_memory`'s shortcut, the highest-risk line here.

    `adventure.actions` returns every branch's actions. Slicing it without
    the path would assemble a prompt out of two different stories, and
    nothing would raise an error. This test loads the collection
    deliberately and checks that the answer is still the path.
    """
    db, adventure, _ = forked
    loaded = list(adventure.actions)  # every branch, ordered by index
    assert len(loaded) == 11  # the path's 8, plus A4, A5 and B6
    assert labels(history.story_actions(adventure)) == [
        "A0", "A1", "A2", "A3", "B4", "B5", "C6", "C7"
    ]
    assert history.count(adventure) == 8
    assert labels(history.tail(adventure, 3)) == ["B5", "C6", "C7"]


def test_user_scripts_are_handed_the_path(forked):
    """The same risk one layer up, in code visible to users:
    `pipeline._history()` is the documented scripting history API."""
    db, adventure, _ = forked
    list(adventure.actions)  # the pipeline's caller has usually loaded these
    pipeline = ScriptPipeline(adventure, db)
    assert [h["text"] for h in pipeline._history()] == [
        "A0", "A1", "A2", "A3", "B4", "B5", "C6", "C7"
    ]
    assert pipeline._info()["actionCount"] == 8


# ------------------------------------------------------------ over the wire

@pytest.fixture()
def client(forked, monkeypatch):
    db, adventure, ids = forked
    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)

    def _current_user(session=Depends(get_db)):
        return session.get(models.User, ids["user"])

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    try:
        yield c, db, adventure, ids
    finally:
        app.dependency_overrides.clear()


def test_the_page_the_reader_opens_is_the_path(client):
    c, db, adventure, ids = client
    r = c.get(f"/api/adventures/{adventure.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["text"] for a in body["actions"]] == [
        "A0", "A1", "A2", "A3", "B4", "B5", "C6", "C7"
    ]
    # `action_count` tells the reader whether more actions exist above. It
    # counts the path too: 8, not the 13 rows the adventure holds.
    assert body["action_count"] == 8


def test_paging_up_walks_the_path_across_the_forks(client):
    c, db, adventure, ids = client
    first = c.get(f"/api/adventures/{adventure.id}/actions", params={"limit": 3}).json()
    assert [a["text"] for a in first["actions"]] == ["B5", "C6", "C7"]
    assert first["has_more"] is True
    older = c.get(
        f"/api/adventures/{adventure.id}/actions",
        params={"limit": 3, "before_id": first["actions"][0]["id"]},
    ).json()
    assert [a["text"] for a in older["actions"]] == ["A2", "A3", "B4"]
    oldest = c.get(
        f"/api/adventures/{adventure.id}/actions",
        params={"limit": 3, "before_id": older["actions"][0]["id"]},
    ).json()
    assert [a["text"] for a in oldest["actions"]] == ["A0", "A1"]
    assert oldest["has_more"] is False


def test_paging_from_an_anchor_off_the_path_reports_the_end(client):
    """A stale client holding an id from another branch gets an empty page,
    not that branch's story."""
    c, db, adventure, ids = client
    off_path = (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adventure.id,
                models.Action.text == "A5")
        .one()
    )
    page = c.get(
        f"/api/adventures/{adventure.id}/actions",
        params={"before_id": off_path.id},
    ).json()
    assert page["actions"] == []
    assert page["has_more"] is False


def test_the_index_screen_quotes_the_branch_being_played(client):
    c, db, adventure, ids = client
    listed = {row["id"]: row for row in c.get("/api/adventures").json()}
    # The newest *narrated* node on C: do/say are the player's voice, and the
    # fixture alternates types, so C7 is the one that reads as story.
    assert listed[adventure.id]["snippet"] == "C7"
    switch_to(db, adventure, ids["a"], 5)
    listed = {row["id"]: row for row in c.get("/api/adventures").json()}
    assert listed[adventure.id]["snippet"] == "A5"
    assert listed[ids["other"]]["snippet"] == "X3"


# ------------------------------------------------------- the flush guard

def test_a_node_written_without_a_branch_is_placed_anyway(forked):
    """SP1 wired the writers. Since SP2, an unplaced node is an invisible one.

    This behavior lets a fixture, a script, or a test built straight through
    the ORM keep working. It is also why the baseline contract still passes
    with its actions written directly to the database.
    """
    db, adventure, ids = forked
    written = models.Action(
        adventure_id=adventure.id, index=99, type="do", text="C8"
    )
    db.add(written)
    db.commit()
    assert written.branch_id == ids["c"]
    assert written.depth == 99
    assert adventure.head_depth == 99
    assert labels(history.tail(adventure, 2)) == ["C7", "C8"]


def test_a_memory_written_without_a_branch_is_placed_anyway(forked):
    db, adventure, ids = forked
    memory = models.Memory(
        adventure_id=adventure.id, text="The cave was cold.", source_start=0, source_end=3
    )
    db.add(memory)
    db.commit()
    assert memory.branch_id == ids["c"]
    assert memory.depth == 3


def test_placing_a_flush_of_nodes_reads_the_branch_once(forked, emitted_sql):
    """The guard resolves the head once per flush, not once per node.

    The identity map holds weak references. A branch row with no strong
    reference gets collected between two nodes and read back again for the
    next one. Writing two hundred actions in one flush ran two hundred
    SELECTs on `branches` before the head lookup moved outside the loop.
    The test result alone would not have shown this.
    """
    db, adventure, _ = forked
    emitted_sql.clear()
    for i in range(50):
        db.add(models.Action(
            adventure_id=adventure.id, index=500 + i, type="do", text=f"bulk {i}"
        ))
    db.commit()
    branch_reads = [s for s in emitted_sql if s.startswith("SELECT") and "FROM branches" in s]
    assert len(branch_reads) <= 2, (
        f"{len(branch_reads)} reads of `branches` to place 50 nodes"
    )


def test_an_adventure_with_no_branch_at_all_reads_as_empty(forked):
    """A missing branch must fail loudly: nothing, rather than everything.

    A row with no branch cannot be shown without guessing which story it
    belongs to, and a wrong guess here puts a sibling's turns into a
    prompt.
    """
    db, adventure, ids = forked
    stray = make_adventure(db, db.get(models.User, ids["user"]), "Stray")
    db.query(models.Action).filter(models.Action.text == "A0").update(
        {"adventure_id": stray.id}, synchronize_session=False
    )
    db.commit()
    db.expire_all()
    stray = db.get(models.Adventure, stray.id)
    assert lineage.path_of(db, stray).entries == []
    assert history.story_actions(stray) == []
    assert history.count(stray) == 0


# --------------------------------------------------- the cost of forking

@pytest.fixture()
def deeply_forked():
    """A story forked twenty times, then played forty turns past the last one.

    This shape tests the design's core assumption: reading the tail of this
    story must cost the same as reading the tail of an unforked story,
    because the window is covered long before the ancestry runs out.
    """
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="deep@example.com")
    db.add(user)
    db.flush()
    adventure = make_adventure(db, user, "Deep")
    branch = make_branch(db, adventure)
    depth = 0
    for _ in range(4):
        add_node(db, adventure, branch, depth, "n")
        depth += 1
    for fork in range(20):
        branch = make_branch(db, adventure, parent=branch, fork_depth=depth - 1)
        for _ in range(2):
            add_node(db, adventure, branch, depth, "n")
            depth += 1
    tip_branch = make_branch(db, adventure, parent=branch, fork_depth=depth - 1)
    for _ in range(40):
        add_node(db, adventure, tip_branch, depth, "n")
        depth += 1
    adventure.head_branch_id = tip_branch.id
    adventure.head_depth = depth - 1
    db.commit()
    try:
        yield db, adventure
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def emitted_sql():
    """Every statement the connection runs, so a clause can be counted."""
    seen = []

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", on_execute)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", on_execute)


def branch_terms(statement: str) -> int:
    return statement.count("actions.branch_id =")


def test_a_tail_read_names_one_branch_however_many_forks_there_were(
    deeply_forked, emitted_sql
):
    db, adventure = deeply_forked
    assert len(lineage.path_of(db, adventure)) == 22  # the whole ancestry
    emitted_sql.clear()
    rows = history.tail(adventure, 32)
    assert len(rows) == 32
    selects = [s for s in emitted_sql if "FROM actions" in s and branch_terms(s)]
    assert selects, "no action read was emitted"
    # Clause count is bounded by the context window, not by fork count: the
    # newest branch alone holds forty turns, so one entry covers a window of
    # thirty-two and the other twenty-one are never named.
    assert max(branch_terms(s) for s in selects) == 1


def test_a_window_reaching_past_the_forks_names_only_what_it_needs(
    deeply_forked, emitted_sql
):
    db, adventure = deeply_forked
    emitted_sql.clear()
    rows = history.tail(adventure, 41)  # 40 on the tip branch, one older
    assert len(rows) == 41
    selects = [s for s in emitted_sql if "FROM actions" in s and branch_terms(s)]
    # Two lineage entries reach 41 deep (40 + 2). The other twenty stay
    # unnamed. Every fork past the window costs the query nothing.
    assert max(branch_terms(s) for s in selects) == 2


def test_the_estimate_is_arithmetic_not_a_query(deeply_forked):
    db, adventure = deeply_forked
    path = lineage.path_of(db, adventure)
    assert path.prefix_covering(1) == 1
    assert path.prefix_covering(40) == 1
    assert path.prefix_covering(41) == 2
    assert path.prefix_covering(43) == 3
    assert path.prefix_covering(10 ** 6) == len(path)


def test_forking_twenty_times_costs_the_same_bytes_as_never_forking(
    deeply_forked,
):
    """The design's cost assumption, measured in bytes.

    Two stories of the same length, one played straight through and one
    forked twenty times, cost the same to read their newest window. The
    window is covered by the newest lineage entry either way, and the
    ancestry is never named. The forked read pays for one extra row: the
    branch it reads the lineage from.
    """
    db, forked_adventure = deeply_forked
    flat = make_adventure(db, db.get(models.User, forked_adventure.user_id), "Flat")
    branch = make_branch(db, flat)
    for depth in range(84):  # the same 84 nodes the forked story is long
        add_node(db, flat, branch, depth, "n")
    flat.head_branch_id = branch.id
    flat.head_depth = 83
    flat_id, forked_id = flat.id, forked_adventure.id
    # Commit and release the connection. The meter wraps the connection
    # pool's factory, so a connection checked out before the meter attaches
    # is never visible to it. Building the fixture is a write path the test
    # does not measure, and it is not charged to either scope.
    db.commit()
    db.expire_all()

    meter = dbmeter.Meter()
    meter.attach(engine)
    try:
        with meter.scope("flat"):
            assert len(history.tail(db.get(models.Adventure, flat_id), 32)) == 32
        flat_bytes = meter.scopes[-1].total.fetched
        with meter.scope("forked"):
            assert len(history.tail(db.get(models.Adventure, forked_id), 32)) == 32
        forked_bytes = meter.scopes[-1].total.fetched
    finally:
        meter.detach()

    assert flat_bytes > 0, "the meter saw nothing; it is measuring the wrong connection"

    assert forked_bytes < flat_bytes * 1.25, (
        f"reading a 20-fork story cost {forked_bytes:,} B against the "
        f"{flat_bytes:,} B an unforked one of the same length cost"
    )


def test_a_gap_in_the_story_widens_the_read_rather_than_shortening_it(
    deeply_forked, emitted_sql
):
    """The estimate counts depths, and a deleted action leaves a depth with
    no row behind it. The read must notice it came up short and widen."""
    db, adventure = deeply_forked
    victim = (
        db.query(models.Action)
        .filter(models.Action.branch_id == adventure.head_branch_id)
        .order_by(models.Action.depth)
        .first()
    )
    db.delete(victim)
    db.commit()
    emitted_sql.clear()
    rows = history.tail(adventure, 40)
    assert len(rows) == 40  # 39 on the tip branch, one borrowed from above
    selects = [s for s in emitted_sql if "FROM actions" in s and branch_terms(s)]
    assert max(branch_terms(s) for s in selects) == len(lineage.path_of(db, adventure))
