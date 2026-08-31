"""Memories must never describe narration that is no longer in the story, and
must never skip a stretch of it.

For six phases, the answer was a holdback. Summarization stopped one action
short of the newest, because only the last action was retryable, and a
retry rewrote `Action.text` under a mark that had already moved past it.
SP4 ended that: a retry writes a sibling node, and the coordinate's derived
work is withdrawn as it happens, using the same repair that undo and delete
already made. Correctness has rested on that withdrawal ever since, and the
second half of this file is where it is asserted.

The first half is about what the withdrawal costs. Redoing a block is
correct and it is also paid for twice, so `memorybank.SETTLE_SLACK` keeps a
block from ending on the newest action, which is the only action retry and
take-switching can reach. A block forms as soon as one action has settled
past it, and changing what a coordinate says still takes back what was
derived from it.

Phase 14 SP3 changed what the mark is. It used to be a count of covered
story actions, and the second half of this file is the cost of that.
Deleting an action from in front of a position slid a never-summarized
action into the covered range, so every delete had to slide the cursors
too. The mark is a node now, `(branch_id, depth)`, and a node does not move
when something in front of it is deleted. Those tests now assert that
nothing happens, where they used to assert that the right correction
happened.

    python -m pytest tests/test_memory_settling.py -v
"""
import asyncio


import pytest

from app import memorybank, models, tree
from app.context import cursors, history
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
            adventure_id=adventure.id,
            type="ai" if i % 2 else "do", text=f"Action {i}.",
        ))
    db.commit()
    db.refresh(adventure)
    return adventure


def cover(db, adventure, position: int) -> None:
    """Mark the first `position` story actions as already summarized.

    Written as a position and translated to the node it names, because that is
    what every adventure in the database looked like before SP3 and what a v1
    bundle still carries.
    """
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


# ------------------------------------------- when a block forms (SETTLE_SLACK)

def test_a_block_that_ends_on_the_newest_action_waits(db, monkeypatch):
    """Covered to action 5 with 12 actions: block 6-11 is full, but it ends on
    the newest action, so it is not written yet.

    Writing it would be correct. A retry of node 11 withdraws it on its way
    past, which is the same repair as
    `test_deleting_a_summarized_node_withdraws_its_memory`. It would also mean
    the block was summarized once, thrown away, and summarized again, for a
    memory that says nothing the history window does not still carry.
    """
    adventure = make_adventure(db, 12)
    cover(db, adventure, 6)

    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)

    assert stub.excerpts == []
    assert covered_depth(db, adventure) == 5  # the mark stays where it was

    # One more action settles the block. It is written from the same six: the
    # slack asks for story past the block, it does not grow the block.
    db.add(models.Action(adventure_id=adventure.id, type="ai", text="Action 12."))
    db.commit()
    db.refresh(adventure)
    run_memories(db, adventure, stub, monkeypatch)

    assert len(stub.excerpts) == 1
    assert "Action 11." in stub.excerpts[0]
    assert "Action 12." not in stub.excerpts[0]
    memory = db.query(models.Memory).one()
    assert (memory.source_start, memory.source_end) == (6, 11)
    # The mark and the memory name the same node. That is what keeps them
    # from drifting apart, however gappy the underlying depths are.
    assert (memory.branch_id, memory.depth) == cursors.MEMORY.stored(adventure)
    assert covered_depth(db, adventure) == 11


def test_a_retry_at_the_tip_has_no_memory_to_withdraw(db, monkeypatch):
    """The slack, stated as the case it removes.

    Retry and take-switching both refuse anything but the newest action
    (`takes.retry_action`, `takes.switch_take`), so holding the block end one
    action back puts every memory out of their reach. Nothing here is a claim
    about the withdrawal path, which undo and delete still reach at any node.
    """
    adventure = make_adventure(db, 13)
    run_memories(db, adventure, StubSummarizer(), monkeypatch)

    assert db.query(models.Memory).count() == 2  # blocks 0-5 and 6-11
    assert covered_depth(db, adventure) == 11

    tip = history.newest(adventure)
    assert tip.depth == 12  # the deepest memory is a node behind it
    assert memorybank.forget_node(db, adventure, tip) == 0
    assert covered_depth(db, adventure) == 11  # so a retry rewinds nothing


def test_the_first_memory_lands_at_memory_start(db, monkeypatch):
    adventure = make_adventure(db, memorybank.MEMORY_START - 1)
    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)
    assert stub.excerpts == []  # too short to have started at all

    db.add(models.Action(
        adventure_id=adventure.id,
        type="do", text="Later.",
    ))
    db.commit()
    db.refresh(adventure)
    run_memories(db, adventure, stub, monkeypatch)
    # MEMORY_START is 12 actions = two full blocks. The first is settled,
    # because the second sits past it. The second ends on the newest action
    # and waits.
    assert len(stub.excerpts) == 1
    assert "Later." not in stub.excerpts[-1]
    assert covered_depth(db, adventure) == memorybank.MEMORY_INTERVAL - 1

    # One action past the second block settles it too, and both are caught up
    # in one run (MAX_MEMORIES_PER_RUN allows 5).
    db.add(models.Action(adventure_id=adventure.id, type="ai", text="Even later."))
    db.commit()
    db.refresh(adventure)
    run_memories(db, adventure, stub, monkeypatch)
    assert len(stub.excerpts) == 2
    assert "Later." in stub.excerpts[-1]
    assert covered_depth(db, adventure) == memorybank.MEMORY_START - 1


def test_legacy_caught_up_adventure_is_not_rewound(db, monkeypatch):
    """An adventure summarized under the old rule carries a cursor equal to
    its action count, one past the end of the story. That used to require a
    clamp on every post-turn pass, and clamping it to the settled count
    re-covered an action.

    A mark that names a node has no such edge. The newest action is the
    node, and "everything after it" is empty until the story grows.
    """
    adventure = make_adventure(db, 12)
    db.add(models.Memory(adventure_id=adventure.id, text="A", source_start=0, source_end=5))
    db.add(models.Memory(adventure_id=adventure.id, text="B", source_start=6, source_end=11))
    cover(db, adventure, 12)

    assert covered_depth(db, adventure) == 11  # the newest action, not one past it
    assert history.count_after(adventure, covered_depth(db, adventure)) == 0

    # Grow the story and let the next block form.
    for i in range(12, 25):
        db.add(models.Action(adventure_id=adventure.id, type="do", text=f"Action {i}."))
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
            adventure_id=adventure.id, depth=end
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
    # Node 4 is inside memory A's block but is not the node it hangs off,
    # so nothing is withdrawn. The old code read it the same way: only a
    # memory whose end had fallen off the story was pruned.
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, depth=4).one()

    assert memorybank.forget_node(db, adventure, victim) == 0
    db.delete(victim)
    db.commit()
    db.refresh(adventure)

    assert covered_depth(db, adventure) == 11
    assert [m.text for m in db.query(models.Memory).all()] == ["A", "B"]
    assert orphans(db, adventure) == []


def test_deleting_a_later_action_leaves_the_mark_alone(db):
    adventure = summarized_adventure(db)
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, depth=12).one()

    memorybank.forget_node(db, adventure, victim)
    db.delete(victim)
    db.commit()
    db.refresh(adventure)

    assert covered_depth(db, adventure) == 11
    assert orphans(db, adventure) == []


def test_deleting_a_summarized_node_withdraws_its_memory(db):
    """Discarding the memory is not enough. The story it covered is still
    behind the mark, so the mark has to move back to where that block began.

    Memory B ends on node 11, so deleting node 11 withdraws it. The old code
    found this by scanning for a memory whose covered range had fallen off
    the end of the story. Now the memory hangs off the node, so finding it
    is a lookup.
    """
    adventure = summarized_adventure(db)
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, depth=11).one()

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
