"""The story summary is a row on the tree, and behaves as a memory does.

This file guards one defect. The summary used to be one text column on the
adventure, so it had no coordinate and nothing could withdraw it. Deleting the
turn whose summarizer run produced a bad summary left the text in place, and
forking away from that turn read the same row. Neither result was recoverable,
because the rewrite is incremental and every later version was built from the
bad one.

Each claim below fails silently in production if it is wrong. The app never
shows the summary next to the turn that wrote it, so a stale summary reads as a
summary that is merely poor.

These tests build the fork by hand, as `test_memory_nodes.py` does, so that they
cannot pass by agreeing with a defect in the fork code.

    python -m pytest tests/test_summary_versions.py -v
"""
import asyncio

import pytest

from app import bundle, memorybank, models, summaries
from app.context import cursors, lineage
from app.database import Base, SessionLocal, engine
from app.routers.adventures import nodes as node_routes


# --------------------------------------------------------------- the fixture

def make_branch(db, adventure, parent=None, fork_depth=None):
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


def add_node(db, adventure, branch, depth, label):
    action = models.Action(
        adventure_id=adventure.id,
        branch_id=branch.id,
        depth=depth,
        type="ai" if depth % 2 else "do",
        text=f"{label}{depth} the road bends onward past the treeline.",
    )
    db.add(action)
    return action


class Stub:
    """A summarizer that records its prompts and returns fixed text."""

    def __init__(self, *replies):
        self.replies = list(replies) or ["A summary."]
        self.prompts = []

    async def complete(self, system, user, **kwargs):
        self.prompts.append(user)
        return self.replies[min(len(self.prompts), len(self.replies)) - 1]


@pytest.fixture()
def story():
    """A0..A7 on one branch, with the head at A7.

    A test that needs a fork makes one from this fixture with `fork_at`.
    """
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="summary@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(user_id=user.id, api_key="enc:dummy", model="m")
    db.add(settings)
    adventure = models.Adventure(
        user_id=user.id, title="Versions", script_state={}, auto_summarize=True,
    )
    db.add(adventure)
    db.flush()
    a = make_branch(db, adventure)
    nodes = {depth: add_node(db, adventure, a, depth, "A") for depth in range(8)}
    adventure.head_branch_id = a.id
    adventure.head_depth = 7
    db.commit()
    try:
        yield db, adventure, settings, a, nodes
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def fork_at(db, adventure, parent, depth, count=2):
    """A branch off `parent` at `depth`, holding `count` nodes of its own."""
    branch = make_branch(db, adventure, parent=parent, fork_depth=depth)
    for i in range(count):
        add_node(db, adventure, branch, depth + 1 + i, "B")
    adventure.head_branch_id = branch.id
    adventure.head_depth = depth + count
    db.commit()
    return branch


# ------------------------------------------------------ what a delete undoes

def test_deleting_the_turn_a_summary_was_written_at_brings_back_the_one_before(
    story
):
    """The reported defect, in one test.

    The player dislikes the summary a turn produced, deletes the AI message, and
    continues. Before this change, the next prompt still carried the deleted
    turn's summary, because the text was a column and no coordinate named it.
    """
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "Older, and fine.", node=nodes[3], source_start=0)
    summaries.record(db, adventure, "Newer, and wrong.", node=nodes[7], source_start=4)
    cursors.SUMMARY.anchor_at(adventure, nodes[7])
    db.commit()
    assert adventure.story_summary == "Newer, and wrong."

    node_routes.delete_turn(db, adventure, nodes[7])
    db.delete(nodes[7])
    db.commit()

    assert adventure.story_summary == "Older, and fine."
    assert cursors.SUMMARY.stored(adventure) == (a.id, 3), (
        "the cursor has to return the stretch the withdrawn version covered, "
        "or the pass counts that stretch as summarized while no version "
        "describes it"
    )


def test_a_delete_that_touches_no_version_leaves_the_summary_alone(story):
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "Written at 3.", node=nodes[3], source_start=0)
    cursors.SUMMARY.anchor_at(adventure, nodes[3])
    db.commit()

    node_routes.delete_turn(db, adventure, nodes[6])
    db.delete(nodes[6])
    db.commit()

    assert adventure.story_summary == "Written at 3."
    assert cursors.SUMMARY.stored(adventure) == (a.id, 3)


def test_a_typed_summary_on_the_opening_node_survives_a_delete(story):
    """The exception `memorybank.forget_node` makes for memories applies here.

    A version that folded in no story is one the player typed, and at depth 0 it
    is usually the only summary an adventure has.
    """
    db, adventure, settings, a, nodes = story
    adventure.head_depth = 0
    summaries.set_text(db, adventure, "Typed before anything happened.")
    db.commit()

    node_routes.delete_turn(db, adventure, nodes[0])
    db.commit()

    assert adventure.story_summary == "Typed before anything happened."


# ------------------------------------------------------- what a fork inherits

def test_a_fork_reads_the_version_from_before_it_and_not_the_one_after(story):
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "Before the fork.", node=nodes[3], source_start=0)
    summaries.record(db, adventure, "After the fork.", node=nodes[7], source_start=4)
    db.commit()
    assert adventure.story_summary == "After the fork."

    fork_at(db, adventure, a, depth=3)

    assert adventure.story_summary == "Before the fork.", (
        "the new line read the summary of the line the player left"
    )


def test_a_version_written_on_one_branch_is_invisible_from_its_sibling(story):
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "Shared trunk.", node=nodes[3], source_start=0)
    b = fork_at(db, adventure, a, depth=3)
    summaries.set_text(db, adventure, "Written while playing B.")
    db.commit()
    assert adventure.story_summary == "Written while playing B."

    adventure.head_branch_id = a.id
    adventure.head_depth = 7
    db.commit()

    assert adventure.story_summary == "Shared trunk."
    assert summaries.newest(db, adventure).branch_id == a.id
    assert b.id != a.id


# ------------------------------------------------------- what an edit records

def test_a_typed_summary_is_anchored_at_the_head(story):
    db, adventure, settings, a, nodes = story
    summaries.set_text(db, adventure, "What the player typed.")
    db.commit()
    row = summaries.newest(db, adventure)
    assert (row.branch_id, row.depth) == (a.id, 7)
    assert row.hand_edited is True
    assert row.source_start is None, "a typed version folds in no stretch of story"


def test_editing_twice_at_one_coordinate_makes_one_version(story):
    """The field saves on a debounce, so one editing session sends several
    requests. If each request added a version, the table would fill with the
    prefixes of a sentence."""
    db, adventure, settings, a, nodes = story
    for text in ("The", "The hero", "The hero left."):
        summaries.set_text(db, adventure, text)
        db.commit()
    assert adventure.story_summary == "The hero left."
    assert db.query(models.Summary).count() == 1


def test_editing_the_version_a_run_wrote_keeps_where_it_started_reading(story):
    """Rewriting the text does not unclaim the story the version folded in. If
    it did, deleting the turn would leave the cursor past a stretch that no
    version describes."""
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "What the model wrote.", node=nodes[7],
                     source_start=4)
    db.commit()

    summaries.set_text(db, adventure, "What the player prefers.")
    db.commit()

    row = summaries.newest(db, adventure)
    assert db.query(models.Summary).count() == 1
    assert row.text == "What the player prefers."
    assert row.hand_edited is True
    assert row.source_start == 4


def test_an_edit_at_a_new_coordinate_leaves_the_older_version_in_place(story):
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "Written at 3.", node=nodes[3], source_start=0)
    db.commit()
    summaries.set_text(db, adventure, "Typed at 7.")
    db.commit()

    assert db.query(models.Summary).count() == 2
    assert adventure.story_summary == "Typed at 7."


# ---------------------------------------------------------- what a run writes

def test_the_pass_adds_a_version_instead_of_overwriting(story, monkeypatch):
    db, adventure, settings, a, nodes = story
    monkeypatch.setattr(memorybank, "SUMMARY_INTERVAL", 1)
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: Stub("Updated."))
    summaries.record(db, adventure, "Old text.", node=nodes[3], source_start=0)
    cursors.SUMMARY.anchor_at(adventure, nodes[3])
    db.commit()

    asyncio.run(memorybank._update_story_summary(adventure, settings, db))

    assert adventure.story_summary == "Updated."
    assert db.query(models.Summary).count() == 2
    row = summaries.newest(db, adventure)
    assert (row.branch_id, row.depth) == (a.id, 7)
    assert row.source_start == 4, "the version starts at the depth after the mark"
    assert row.hand_edited is False


def test_the_pass_rewrites_from_the_version_the_player_typed(story, monkeypatch):
    db, adventure, settings, a, nodes = story
    stub = Stub("Folded in.")
    monkeypatch.setattr(memorybank, "SUMMARY_INTERVAL", 1)
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    summaries.set_text(db, adventure, "What the player typed.")
    db.commit()

    asyncio.run(memorybank._update_story_summary(adventure, settings, db))

    [prompt] = stub.prompts
    assert "What the player typed." in prompt


# ------------------------------------------------------------- the rebuild

def test_regenerate_ignores_the_current_text(story, monkeypatch):
    """The reason the button exists. The incremental pass sends the model the
    current summary and asks for an updated version, so a bad version becomes
    the base of every version after it."""
    db, adventure, settings, a, nodes = story
    stub = Stub("Rebuilt from the story.")
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    summaries.record(db, adventure, "Corrupted beyond saving.", node=nodes[3],
                     source_start=0)
    cursors.SUMMARY.anchor_at(adventure, nodes[3])
    db.add(models.Memory(
        adventure_id=adventure.id, text="The hero left the village.",
        branch_id=a.id, depth=3, source_start=0, source_end=3,
    ))
    db.commit()

    asyncio.run(memorybank.regenerate(adventure, settings, db))

    prompt = stub.prompts[-1]
    assert "Corrupted beyond saving." not in prompt
    assert "(none yet)" in prompt
    assert "The hero left the village." in prompt
    assert adventure.story_summary == "Rebuilt from the story."


def test_regenerate_keeps_the_version_it_replaced(story, monkeypatch):
    """Deleting the new row then reverses a rebuild that comes out worse."""
    db, adventure, settings, a, nodes = story
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: Stub("Rebuilt."))
    summaries.record(db, adventure, "The old one.", node=nodes[3], source_start=0)
    db.commit()

    asyncio.run(memorybank.regenerate(adventure, settings, db))

    texts = [row.text for row in db.query(models.Summary).order_by(models.Summary.id)]
    assert texts == ["The old one.", "Rebuilt."]
    row = summaries.newest(db, adventure)
    assert row.source_start == 0, "a rebuild reads the story from its beginning"
    assert cursors.SUMMARY.stored(adventure) == (a.id, 7)


def test_regenerate_reads_the_story_when_there_is_no_memory_bank(story, monkeypatch):
    """An adventure played with auto-summarization off has no bank, and it is
    the adventure whose summary most needs rebuilding."""
    db, adventure, settings, a, nodes = story
    stub = Stub("Digested.", "Digested.", "Rebuilt from raw story.")
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    monkeypatch.setattr(memorybank, "DIGEST_CHUNK_TOKENS", 12)
    monkeypatch.setattr(memorybank, "MAX_DIGEST_CHUNKS", 30)

    asyncio.run(memorybank.regenerate(adventure, settings, db))

    assert len(stub.prompts) > 1, "the story was never chunked"
    assert "A0 the road bends" in stub.prompts[0], "a chunk holds raw story text"
    assert adventure.story_summary == "Rebuilt from raw story."


def test_the_rebuild_caps_how_many_calls_one_click_costs(story, monkeypatch):
    """A fixed chunk size would leave the call count unbounded, and the button
    would cost a few cents on one save and a few dollars on another."""
    db, adventure, settings, a, nodes = story
    stub = Stub("Digested.")
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    monkeypatch.setattr(memorybank, "DIGEST_CHUNK_TOKENS", 1)
    monkeypatch.setattr(memorybank, "MAX_DIGEST_CHUNKS", 3)

    asyncio.run(memorybank.regenerate(adventure, settings, db))

    # The chunks, plus the one fold at the end.
    assert len(stub.prompts) <= 4, f"{len(stub.prompts)} calls for one click"


def test_regenerate_refuses_an_adventure_with_no_story(story, monkeypatch):
    db, adventure, settings, a, nodes = story
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: Stub("x"))
    for node in nodes.values():
        db.delete(node)
    adventure.head_depth = lineage.NO_DEPTH
    db.commit()

    with pytest.raises(ValueError):
        asyncio.run(memorybank.regenerate(adventure, settings, db))


# ----------------------------------------------------------------- the file

def test_a_bundle_round_trip_keeps_every_version_where_it_was(story):
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "Older.", node=nodes[3], source_start=0)
    summaries.set_text(db, adventure, "Newer, typed.")
    db.commit()

    payload = bundle.export(db, adventure)
    story_plan = bundle.plan(payload, bundle.check_format(payload))
    copy = bundle.materialize(db, payload, story_plan, adventure.user_id)
    db.commit()

    rows = (
        db.query(models.Summary)
        .filter(models.Summary.adventure_id == copy.id)
        .order_by(models.Summary.depth)
        .all()
    )
    assert [(r.text, r.depth, r.hand_edited) for r in rows] == [
        ("Older.", 3, False), ("Newer, typed.", 7, True),
    ]
    assert copy.story_summary == "Newer, typed."


def test_a_file_that_predates_the_table_keeps_its_one_summary(story):
    """An export written by an older build has `storySummary` and no
    `summaries`. That text is the whole summary the file holds, so it has to
    survive the import."""
    db, adventure, settings, a, nodes = story
    summaries.record(db, adventure, "The only summary.", node=nodes[3], source_start=0)
    cursors.SUMMARY.anchor_at(adventure, nodes[3])
    db.commit()

    payload = bundle.export(db, adventure)
    del payload["summaries"]
    story_plan = bundle.plan(payload, bundle.check_format(payload))
    copy = bundle.materialize(db, payload, story_plan, adventure.user_id)
    db.commit()

    assert copy.story_summary == "The only summary."
    row = (
        db.query(models.Summary)
        .filter(models.Summary.adventure_id == copy.id)
        .one()
    )
    assert row.depth == 3, "the row goes where the summary anchor says it read to"
    assert row.source_start == 0, (
        "a summary built by repeated updates read the story from its beginning, "
        "so a withdrawal has to rewind that far"
    )


# --------------------------------------------------------------- the endpoints

@pytest.fixture()
def client(monkeypatch):
    """The API, signed in as the owner of an adventure with a short story."""
    from fastapi import Depends
    from fastapi.testclient import TestClient

    from app import auth, limits
    from app.database import get_db
    from app.main import app

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="api@example.com")
    db.add(user)
    db.flush()
    db.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="m"))
    adventure = models.Adventure(
        user_id=user.id, title="A", script_state={}, world_state={},
    )
    db.add(adventure)
    db.flush()
    branch = make_branch(db, adventure)
    for depth in range(4):
        add_node(db, adventure, branch, depth, "A")
    adventure.head_branch_id = branch.id
    adventure.head_depth = 3
    db.commit()
    user_id, adventure_id = user.id, adventure.id
    db.close()

    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)
    monkeypatch.setattr(auth, "resolve_provider_config",
                        lambda s: _NotDemo())

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    try:
        yield c, adventure_id
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


class _NotDemo:
    using_demo = False


def test_patching_the_summary_writes_a_version_and_reads_it_back(client):
    """`AdventureUpdate` still carries `story_summary`, so the field in the plot
    panel is unchanged. The PATCH handler routes the value into a row."""
    c, adventure_id = client
    r = c.patch(f"/api/adventures/{adventure_id}",
                json={"story_summary": "What the player typed."})
    assert r.status_code == 200
    assert r.json()["story_summary"] == "What the player typed."
    assert c.get(f"/api/adventures/{adventure_id}").json()["story_summary"] == (
        "What the player typed."
    )


def test_patching_another_field_leaves_the_summary_alone(client):
    c, adventure_id = client
    c.patch(f"/api/adventures/{adventure_id}", json={"story_summary": "Kept."})
    r = c.patch(f"/api/adventures/{adventure_id}", json={"title": "Renamed"})
    assert r.status_code == 200
    assert r.json()["story_summary"] == "Kept."
    assert r.json()["title"] == "Renamed"


def test_the_regenerate_endpoint_returns_the_rebuilt_adventure(client, monkeypatch):
    c, adventure_id = client
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: Stub("Rebuilt."))
    c.patch(f"/api/adventures/{adventure_id}", json={"story_summary": "Wrong."})

    r = c.post(f"/api/adventures/{adventure_id}/summary/regenerate")

    assert r.status_code == 200, r.text
    assert r.json()["story_summary"] == "Rebuilt."
