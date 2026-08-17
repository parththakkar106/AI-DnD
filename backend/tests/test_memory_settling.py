"""Memories must never describe an attempt the player can still retry away,
and must never skip a stretch of story.

Only the last action is retryable, so summarization holds the newest action
back one turn (memorybank.settled_story_actions). Without that, a memory could
cover the just-generated AI turn; retrying it rewrites Action.text but the mark
has already moved past it, so the memory is never regenerated and goes on
describing narration that is no longer in the story.

Phase 14 SP3 changed what that mark *is*. It used to be a count of covered
story actions, and the second half of this file is the price of that: deleting
an action from in front of a position slid a never-summarized action into the
covered range, so every delete had to slide the cursors too. The mark is a node
now — `(branch_id, depth)` — and a node does not move when something in front
of it is deleted, so those tests assert that nothing happens where they used to
assert that the right correction happened.

    python -m pytest tests/test_memory_settling.py -v
"""
import asyncio
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest

from app import memorybank, models, tree
from app.context import cursors
from app.database import Base, SessionLocal, engine


class StubSummarizer:
    """Records every excerpt handed to the summarizer."""

    def __init__(self):
        self.excerpts: list[str] = []

    async def complete(self, system, user, **kwargs):
        self.excerpts.append(user)
        return f"Memory {len(self.excerpts)}."


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def make_adventure(db, action_count: int) -> models.Adventure:
    """An adventure whose actions alternate player/AI, newest last."""
    user = models.User(is_guest=False, email="memory@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model")
    db.add(settings)
    adventure = models.Adventure(
        user_id=user.id, title="Cave", script_state={}, auto_summarize=True
    )
    db.add(adventure)
    db.flush()
    for i in range(action_count):
        db.add(models.Action(
            adventure_id=adventure.id, index=i,
            type="ai" if i % 2 else "do", text=f"Action {i}.",
        ))
    db.commit()
    db.refresh(adventure)
    return adventure


def cover(db, adventure, position: int) -> None:
    """Mark the first `position` story actions as already summarized.

    Written as a position and translated to the node it names, because that is
    what every adventure in the database looked like before SP3 and what a v1
    bundle still carries. `memory_cursor` keeps the old number so the two
    coordinate systems can be compared where a test cares.
    """
    adventure.memory_cursor = position
    adventure.summary_cursor = position
    cursors.anchor_at_position(adventure, cursors.MEMORY, position)
    cursors.anchor_at_position(adventure, cursors.SUMMARY, position)
    db.commit()


def covered_depth(db, adventure) -> int:
    """The memory mark, as a depth on the story being played."""
    return cursors.MEMORY.depth(db, adventure)


def run_memories(db, adventure, stub, monkeypatch):
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    settings = db.query(models.Settings).first()
    asyncio.run(memorybank._create_due_memories(adventure, settings, db))


# ------------------------------------------------------------------- settling

def test_settled_actions_holds_back_the_newest(db):
    adventure = make_adventure(db, 5)
    settled = memorybank.settled_story_actions(adventure)
    assert [a.index for a in settled] == [0, 1, 2, 3]


def test_settled_actions_is_a_prefix_so_cursors_stay_valid(db):
    """The safety property behind the whole approach: dropping the newest
    action can never renumber or skip an earlier one."""
    adventure = make_adventure(db, 9)
    full = memorybank.story_actions(adventure)
    settled = memorybank.settled_story_actions(adventure)
    assert full[: len(settled)] == settled


def test_settled_actions_on_a_one_action_story(db):
    adventure = make_adventure(db, 1)
    assert memorybank.settled_story_actions(adventure) == []


# ------------------------------------------------------- the bug this prevents

def test_memory_never_covers_the_newest_retryable_action(db, monkeypatch):
    """Covered up to action 5 with 12 actions is exactly the case that used to
    bite: the 6-action block ends on the newest action, still retryable."""
    adventure = make_adventure(db, 12)
    cover(db, adventure, 6)

    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)

    assert stub.excerpts == []  # only 11 settled — one short of a block
    assert db.query(models.Memory).count() == 0
    assert covered_depth(db, adventure) == 5


def test_the_block_lands_a_turn_later_without_the_newest_action(db, monkeypatch):
    """One more action and the same block is summarized — minus the new one."""
    adventure = make_adventure(db, 13)
    cover(db, adventure, 6)

    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)

    assert len(stub.excerpts) == 1
    excerpt = stub.excerpts[0]
    assert "Action 11." in excerpt  # the block's real last action
    assert "Action 12." not in excerpt  # the newest, still retryable
    memory = db.query(models.Memory).one()
    assert (memory.source_start, memory.source_end) == (6, 11)
    # The mark and the memory name the same node — that is what keeps them from
    # drifting apart however gappy the depths underneath are.
    assert (memory.branch_id, memory.depth) == cursors.MEMORY.stored(adventure)
    assert covered_depth(db, adventure) == 11


def test_first_memory_waits_one_action_past_memory_start(db, monkeypatch):
    adventure = make_adventure(db, memorybank.MEMORY_START)
    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)
    assert stub.excerpts == []

    db.add(models.Action(
        adventure_id=adventure.id, index=memorybank.MEMORY_START, type="do", text="Later.",
    ))
    db.commit()
    db.refresh(adventure)
    run_memories(db, adventure, stub, monkeypatch)
    # 12 settled actions = two full blocks, caught up in one run (MAX_MEMORIES_
    # PER_RUN allows 5); neither may reach the newly added newest action.
    assert len(stub.excerpts) == 2
    assert not any("Later." in e for e in stub.excerpts)


def test_legacy_caught_up_adventure_is_not_rewound(db, monkeypatch):
    """An adventure summarized under the OLD rule carries a cursor equal to its
    action count — one past the settled end. That used to need a clamp on every
    post-turn pass, and clamping it to the *settled* count re-covered an action.

    A mark that names a node has no such edge: the newest action is the node,
    and "everything after it" is empty until the story grows.
    """
    adventure = make_adventure(db, 12)
    db.add(models.Memory(adventure_id=adventure.id, text="A", source_start=0, source_end=5))
    db.add(models.Memory(adventure_id=adventure.id, text="B", source_start=6, source_end=11))
    cover(db, adventure, 12)

    assert covered_depth(db, adventure) == 11  # the newest action, not one past it
    assert memorybank.settled_after(adventure, covered_depth(db, adventure)) == -1

    # Grow the story and let the next block form.
    for i in range(12, 25):
        db.add(models.Action(adventure_id=adventure.id, index=i, type="do", text=f"Action {i}."))
    db.commit()
    db.refresh(adventure)
    run_memories(db, adventure, StubSummarizer(), monkeypatch)

    covered: dict[int, int] = {}
    for m in db.query(models.Memory).all():
        for i in range(m.source_start, m.source_end + 1):
            covered[i] = covered.get(i, 0) + 1
    assert [i for i, c in covered.items() if c > 1] == []  # no action summarized twice
    assert [i for i in range(max(covered) + 1) if i not in covered] == []  # no gaps


def test_no_memories_before_memory_start(db, monkeypatch):
    adventure = make_adventure(db, 8)
    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)
    assert stub.excerpts == []


# ------------------------------------------- deleting already-summarized ground

def orphans(db, adventure) -> list[int]:
    """Depths the mark calls summarized that no memory describes.

    The failure this whole section is about, stated once: an action behind the
    mark with nothing covering it is never summarized again, and nothing ever
    reports it.
    """
    covered: set[int] = set()
    for m in db.query(models.Memory).filter_by(adventure_id=adventure.id):
        covered |= set(range(m.source_start, m.source_end + 1))
    mark = cursors.MEMORY.depth(db, adventure)
    return [
        a.depth for a in memorybank.story_actions(adventure)
        if a.depth <= mark and a.depth not in covered
    ]


def summarized_adventure(db):
    """13 actions with two memories covering depths 0-11, the mark on node 11."""
    adventure = make_adventure(db, 13)
    for text, start, end in (("A", 0, 5), ("B", 6, 11)):
        node = db.query(models.Action).filter_by(
            adventure_id=adventure.id, index=end
        ).one()
        memory = models.Memory(
            adventure_id=adventure.id, text=text, source_start=start, source_end=end
        )
        tree.attach_memory(memory, node)
        db.add(memory)
    cover(db, adventure, 12)
    db.refresh(adventure)
    return adventure


def test_deleting_a_middle_action_leaves_the_mark_where_it_was(db):
    """The bug that motivated the old machinery, and the reason it is gone.

    A position cursor counted actions from the start, so deleting an earlier
    one slid a never-summarized action into the covered range and every delete
    had to correct for it. A depth is not a count: node 11 is still node 11
    with node 5 gone.
    """
    adventure = summarized_adventure(db)
    # Node 4 is inside memory A's block but is not the node it hangs off, so
    # nothing is withdrawn — the same reading the old code had, where only a
    # memory whose *end* had fallen off the story was pruned.
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, index=4).one()

    assert memorybank.forget_node(db, adventure, victim) == 0
    db.delete(victim)
    db.commit()
    db.refresh(adventure)

    assert covered_depth(db, adventure) == 11
    assert [m.text for m in db.query(models.Memory).all()] == ["A", "B"]
    assert orphans(db, adventure) == []


def test_deleting_a_later_action_leaves_the_mark_alone(db):
    adventure = summarized_adventure(db)
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, index=12).one()

    memorybank.forget_node(db, adventure, victim)
    db.delete(victim)
    db.commit()
    db.refresh(adventure)

    assert covered_depth(db, adventure) == 11
    assert orphans(db, adventure) == []


def test_deleting_a_summarized_node_withdraws_its_memory(db):
    """Discarding the memory isn't enough — the story it covered is still
    behind the mark, so the mark has to come back to where that block began.

    Memory B ends on node 11, so deleting node 11 is what withdraws it. The old
    code found this by scanning for a memory whose covered range had fallen off
    the end of the story; the memory hangs off the node now, so it is a lookup.
    """
    adventure = summarized_adventure(db)
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, index=11).one()

    assert memorybank.forget_node(db, adventure, victim) == 1
    db.delete(victim)
    db.commit()
    db.refresh(adventure)

    assert [m.text for m in db.query(models.Memory).all()] == ["A"]
    assert covered_depth(db, adventure) == 5  # back to where the discarded memory began
    assert cursors.SUMMARY.depth(db, adventure) == 5  # and the summary with it
    assert orphans(db, adventure) == []


def test_repeated_deletes_never_orphan_an_action(db):
    """The scenario that motivated this: undo/delete-last, over and over.

    No clamp in the loop any more, and no bookkeeping call per delete beyond
    withdrawing what the node produced.
    """
    adventure = summarized_adventure(db)
    for _ in range(6):
        actions = memorybank.story_actions(adventure)
        if not actions:
            break
        victim = max(actions, key=lambda a: a.depth)
        memorybank.forget_node(db, adventure, victim)
        db.delete(victim)
        db.flush()
        db.expire(adventure, ["actions"])
        db.commit()
        db.refresh(adventure)
        assert orphans(db, adventure) == []
