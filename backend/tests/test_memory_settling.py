"""Memories must never describe an attempt the player can still retry away.

Only the last action is retryable, so summarization holds the newest action
back one turn (memorybank.settled_story_actions). Without that, a memory could
cover the just-generated AI turn; retrying it rewrites Action.text but the
memory cursor has already advanced, so the memory is never regenerated and goes
on describing narration that is no longer in the story.

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

from app import memorybank, models
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
    """cursor=6 with 12 actions is exactly the case that used to bite: the
    6-action block ends on the newest action, which is still retryable."""
    adventure = make_adventure(db, 12)
    adventure.memory_cursor = 6
    db.commit()

    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)

    assert stub.excerpts == []  # only 11 settled — one short of a block
    assert db.query(models.Memory).count() == 0
    assert adventure.memory_cursor == 6


def test_the_block_lands_a_turn_later_without_the_newest_action(db, monkeypatch):
    """One more action and the same block is summarized — minus the new one."""
    adventure = make_adventure(db, 13)
    adventure.memory_cursor = 6
    db.commit()

    stub = StubSummarizer()
    run_memories(db, adventure, stub, monkeypatch)

    assert len(stub.excerpts) == 1
    excerpt = stub.excerpts[0]
    assert "Action 11." in excerpt  # the block's real last action
    assert "Action 12." not in excerpt  # the newest, still retryable
    memory = db.query(models.Memory).one()
    assert (memory.source_start, memory.source_end) == (6, 11)
    assert adventure.memory_cursor == 12


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
    """An adventure summarized under the OLD rule can have memory_cursor equal
    to its action count. The run_post_turn clamp must use the FULL count, not
    the settled one — clamping to settled would rewind the cursor a step and
    re-cover an already-summarized action in the next block."""
    adventure = make_adventure(db, 12)
    db.add(models.Memory(adventure_id=adventure.id, text="A", source_start=0, source_end=5))
    db.add(models.Memory(adventure_id=adventure.id, text="B", source_start=6, source_end=11))
    adventure.memory_cursor = 12
    adventure.summary_cursor = 12
    db.commit()

    # The clamp as run_post_turn applies it.
    count = len(memorybank.story_actions(adventure))
    adventure.memory_cursor = min(adventure.memory_cursor, count)
    assert adventure.memory_cursor == 12  # not rewound to 11

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
    """Action indices the cursor calls summarized that no memory describes."""
    covered: set[int] = set()
    for m in db.query(models.Memory).filter_by(adventure_id=adventure.id):
        covered |= set(range(m.source_start, m.source_end + 1))
    actions = memorybank.story_actions(adventure)
    return [a.index for a in actions[: adventure.memory_cursor] if a.index not in covered]


def summarized_adventure(db):
    """13 actions with two memories covering indices 0-11, cursor at 12."""
    adventure = make_adventure(db, 13)
    db.add(models.Memory(adventure_id=adventure.id, text="A", source_start=0, source_end=5))
    db.add(models.Memory(adventure_id=adventure.id, text="B", source_start=6, source_end=11))
    adventure.memory_cursor = 12
    adventure.summary_cursor = 12
    db.commit()
    db.refresh(adventure)
    return adventure


def test_deleting_a_middle_action_does_not_skip_a_later_one(db):
    """memory_cursor counts positions, so removing an earlier action slides a
    never-summarized one into the covered range unless the cursor slides too."""
    adventure = summarized_adventure(db)
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, index=5).one()

    memorybank.note_action_removed(adventure, victim)
    db.delete(victim)
    db.commit()
    db.refresh(adventure)

    assert adventure.memory_cursor == 11  # slid down by one
    assert orphans(db, adventure) == []


def test_deleting_a_later_action_leaves_cursors_alone(db):
    """Only actions *before* the cursor shift it."""
    adventure = summarized_adventure(db)
    victim = db.query(models.Action).filter_by(adventure_id=adventure.id, index=12).one()

    memorybank.note_action_removed(adventure, victim)
    db.delete(victim)
    db.commit()
    db.refresh(adventure)

    assert adventure.memory_cursor == 12
    assert orphans(db, adventure) == []


def test_pruning_a_memory_rewinds_to_where_it_started(db):
    """Discarding a memory isn't enough — the actions it covered are still
    behind the cursor, so they must be handed back to the summarizer."""
    adventure = summarized_adventure(db)
    # Delete back past index 11, so memory B (6..11) covers a missing action.
    for index in (12, 11):
        victim = db.query(models.Action).filter_by(adventure_id=adventure.id, index=index).one()
        memorybank.note_action_removed(adventure, victim)
        db.delete(victim)
    db.flush()
    db.expire(adventure, ["actions"])

    assert memorybank.prune_dangling_memories(adventure, db) == 1
    db.commit()
    db.refresh(adventure)

    assert [m.text for m in db.query(models.Memory).all()] == ["A"]
    assert adventure.memory_cursor == 6  # back to where the discarded memory began
    assert orphans(db, adventure) == []


def test_repeated_deletes_never_orphan_an_action(db):
    """The scenario that motivated this: undo/delete-last, over and over."""
    adventure = summarized_adventure(db)
    for _ in range(6):
        actions = memorybank.story_actions(adventure)
        if not actions:
            break
        victim = max(actions, key=lambda a: a.index)
        memorybank.note_action_removed(adventure, victim)
        db.delete(victim)
        db.flush()
        db.expire(adventure, ["actions"])
        memorybank.prune_dangling_memories(adventure, db)
        count = len(memorybank.story_actions(adventure))
        adventure.memory_cursor = min(adventure.memory_cursor, count)
        adventure.summary_cursor = min(adventure.summary_cursor, count)
        db.commit()
        db.refresh(adventure)
        assert orphans(db, adventure) == []
        assert adventure.memory_cursor <= len(memorybank.story_actions(adventure))
