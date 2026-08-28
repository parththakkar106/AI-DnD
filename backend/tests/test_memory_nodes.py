"""Phase 14 SP3: memories attach to nodes, and the marks are nodes too.

Two claims, and neither fails loudly if it is wrong:

* A memory belongs to the path that produced it. A memory made on branch B
  must be invisible from A, and the memories of a shared ancestor must be
  visible from both, without anything being copied when a fork happens. The
  failure mode is a prompt that quietly carries a summary of a story the
  player abandoned.
* Retrieval reads the whole lineage, and that stays affordable. The story
  is read through a window, but recall is long-range by definition and
  cannot use one. So the clause names every ancestor. The bet is that
  memories are sparse enough, one per six actions, for that to stay tens of
  small rows even twenty forks deep. This file measures that bet below
  rather than asserting it.

Nothing in the product forks yet, so this file builds the fork by hand,
exactly as `test_branch_clause.py` builds it.

    python -m pytest tests/test_memory_nodes.py -v
"""
import asyncio

import pytest

from app import memorybank, models, tree
from app.context import cursors, lineage
from app.database import Base, SessionLocal, engine
from tools import dbmeter


class StubEmbedder:
    """Returns whatever vector the test set, for any text."""

    def __init__(self, vector=(1.0, 0.0, 0.0)):
        self.vector = list(vector)

    async def embed(self, texts):
        return [list(self.vector) for _ in texts]


# --------------------------------------------------------------- the fixture

def make_branch(db, adventure, parent=None, fork_depth=None):
    """A branch row whose lineage is its parent's lineage, capped, plus
    itself. This is the computation SP5 performs at fork time. The fixture
    reimplements it here so it cannot pass by agreeing with a bug in the
    code under test."""
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
        type="ai" if depth % 2 else "do",
        text=f"{label}{depth}",
    )
    db.add(action)
    return action


def add_memory(db, adventure, text, node, vector=(1.0, 0.0, 0.0), **kwargs):
    """A memory of the block ending on `node`, attached the way the post-turn
    pass attaches one."""
    memory = models.Memory(
        adventure_id=adventure.id, text=text,
        source_start=None if node is None else node.depth,
        source_end=None if node is None else node.depth,
        **kwargs,
    )
    if node is not None:
        tree.attach_memory(memory, node)
    else:
        tree.place_memory(db, adventure, memory)
    db.add(memory)
    db.flush()
    memorybank.set_vector(memory, list(vector))
    db.commit()
    return memory


@pytest.fixture()
def forked():
    """A0..A3, then B4 B5 off A3, then C6 C7 off B5, with a memory attached
    to one node of each branch. A keeps playing past the fork point where B
    left it.

    The head is C, so the story is A0 A1 A2 A3 B4 B5 C6 C7, and the
    memories in play are A's, B's, and C's. The one on A5 is excluded: it
    is on a sibling of B4 and belongs to a story nobody is reading.
    """
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="nodes@example.com")
    db.add(user)
    db.flush()
    settings = models.Settings(
        user_id=user.id, api_key="enc:dummy", model="m",
        embedding_model="text-embedding-3-small", memory_top_k=10,
        memory_bank_capacity=80,
    )
    db.add(settings)
    adventure = models.Adventure(
        user_id=user.id, title="Forked", script_state={}, memory_bank_enabled=True,
        auto_summarize=True,
    )
    db.add(adventure)
    db.flush()

    a = make_branch(db, adventure)
    b = make_branch(db, adventure, parent=a, fork_depth=3)
    c = make_branch(db, adventure, parent=b, fork_depth=5)
    nodes = {}
    for depth in range(4):
        nodes[f"A{depth}"] = add_node(db, adventure, a, depth, "A")
    for depth in (4, 5):  # A kept playing: siblings of B4/B5
        nodes[f"A{depth}"] = add_node(db, adventure, a, depth, "A", index=100 + depth)
    for depth in (4, 5):
        nodes[f"B{depth}"] = add_node(db, adventure, b, depth, "B")
    for depth in (6, 7):
        nodes[f"C{depth}"] = add_node(db, adventure, c, depth, "C")
    db.flush()

    memories = {
        "shared": add_memory(db, adventure, "on the shared trunk", nodes["A3"]),
        "sibling": add_memory(db, adventure, "on A's own continuation", nodes["A5"]),
        "b": add_memory(db, adventure, "on B", nodes["B5"]),
        "c": add_memory(db, adventure, "on C", nodes["C7"]),
    }
    adventure.head_branch_id = c.id
    adventure.head_depth = 7
    db.commit()

    ids = {"a": a.id, "b": b.id, "c": c.id, "nodes": nodes, "memories": memories}
    try:
        yield db, adventure, settings, ids
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def switch_to(db, adventure, branch_id, tip):
    adventure.head_branch_id = branch_id
    adventure.head_depth = tip
    db.commit()


def retrieved(adventure, settings) -> set[str]:
    memorybank.embedding_provider = lambda s: StubEmbedder()
    result = asyncio.run(
        memorybank.retrieve_memories(adventure, settings, update_stats=False)
    )
    assert result["error"] is None, result["error"]
    return {m["text"] for m in result["used"]}


# ------------------------------------------------------------- the isolation

def test_a_memory_on_a_sibling_is_not_retrieved(forked):
    """The whole point of this file. A5 is a node of the story that was
    abandoned when B forked, and the memory attached to it must not reach a
    prompt on C."""
    db, adventure, settings, ids = forked
    assert retrieved(adventure, settings) == {
        "on the shared trunk", "on B", "on C"
    }


def test_a_shared_ancestor_is_visible_from_both_branches(forked):
    """Nothing is copied at a fork, so the trunk's memories are shared by
    construction rather than by duplication."""
    db, adventure, settings, ids = forked
    switch_to(db, adventure, ids["a"], 5)
    from_a = retrieved(adventure, settings)
    assert "on the shared trunk" in from_a
    # From A, the branches taken off it are the ones out of reach.
    assert from_a == {"on the shared trunk", "on A's own continuation"}


def test_the_lineage_is_read_whole_not_windowed(forked):
    """The story is read through a window. Recall is not. The trunk memory
    is four nodes and two forks back, and is still a candidate."""
    db, adventure, settings, ids = forked
    path = lineage.path_of(db, adventure)
    assert len(path) == 3
    # The window a story read would use here names one entry. Retrieval
    # names all three, which is the difference this test checks.
    assert path.prefix_covering(2) == 1
    assert "on the shared trunk" in retrieved(adventure, settings)


def test_a_hand_written_memory_is_anchored_where_it_was_typed(forked):
    """SP7: a typed memory takes the head, so it obeys the same rule as a
    summarized one.

    It used to carry no depth, which sounded like "belongs to the whole
    adventure" and behaved like "cannot be capped at a fork." It followed
    the reader onto branches whose story it never described. Anchoring it
    makes the bank answer one question instead of two.
    """
    db, adventure, settings, ids = forked
    switch_to(db, adventure, ids["a"], 5)
    typed = add_memory(db, adventure, "typed by hand", None)
    assert (typed.branch_id, typed.depth) == (ids["a"], 5), "the head it was typed at"


def test_a_typed_memory_survives_a_fork_of_the_ground_it_was_typed_on(forked):
    """The half of the old behavior that was correct, kept.

    A memory typed on the shared trunk is still there after forking away,
    but only because the fork's path goes through that node, not because
    the memory is exempt from being capped.
    """
    db, adventure, settings, ids = forked
    switch_to(db, adventure, ids["a"], 3)  # the node B, and so C, forked from
    add_memory(db, adventure, "typed on the trunk", None)

    switch_to(db, adventure, ids["c"], 7)
    assert "typed on the trunk" in retrieved(adventure, settings)


def test_a_typed_memory_does_not_follow_you_onto_a_path_it_is_not_on(forked):
    """The other half of the old behavior, which was wrong, is now fixed.

    A5 is A's own continuation past the point where B left it, so it is a
    sibling of the story C tells. This is exactly where the `sibling`
    memory sits, and it is excluded for the same reason. Typing a memory
    instead of summarizing it grants no exemption from the path rule.
    """
    db, adventure, settings, ids = forked
    switch_to(db, adventure, ids["a"], 5)
    add_memory(db, adventure, "typed off the path", None)

    switch_to(db, adventure, ids["c"], 7)
    assert "typed off the path" not in retrieved(adventure, settings)


# ------------------------------------------------------------------ the marks

def test_a_mark_moves_to_the_node_the_memory_covers(forked):
    """The mark and the memory are one statement about where the pass got to,
    so they are written from the same row."""
    db, adventure, settings, ids = forked
    cursors.MEMORY.anchor_at(adventure, ids["nodes"]["B5"])
    db.commit()
    assert cursors.MEMORY.stored(adventure) == (ids["b"], 5)
    assert cursors.MEMORY.depth(db, adventure) == 5


def test_a_mark_from_a_sibling_reads_as_nothing_covered(forked):
    """A mark is a node, so switching to another story must resolve the
    mark's meaning rather than assume it. A path segment this story never
    took is not covered, and the fallback for "not covered" must be redoing
    the work, not skipping it."""
    db, adventure, settings, ids = forked
    cursors.MEMORY.anchor_at(adventure, ids["nodes"]["C7"])
    db.commit()
    switch_to(db, adventure, ids["a"], 5)
    assert cursors.MEMORY.depth(db, adventure) == cursors.NO_DEPTH


def test_a_mark_on_an_ancestor_is_capped_at_the_fork(forked):
    """A6 and A7 are past where this path left A, so a mark deeper than the
    fork cannot mean 'covered' for anything on this story."""
    db, adventure, settings, ids = forked
    cursors.MEMORY.anchor_at(adventure, ids["nodes"]["A5"])
    db.commit()
    assert cursors.MEMORY.depth(db, adventure) == 3  # C forks off B forks off A@3


def test_a_mark_never_moves_forward_on_a_rewind(forked):
    db, adventure, settings, ids = forked
    cursors.MEMORY.anchor_at(adventure, ids["nodes"]["A3"])
    cursors.rewind_all(adventure, ids["c"], 6)
    assert cursors.MEMORY.stored(adventure) == (ids["a"], 3)


# ---------------------------------------------------- what the passes read

def test_the_summary_folds_in_only_the_path_it_is_on(forked, monkeypatch):
    """`_update_story_summary` gathers the memories past its mark. On C
    that is B's and C's memories. It never includes the one on A's own
    continuation, even though that memory's depth would otherwise put it
    inside the range."""
    db, adventure, settings, ids = forked
    monkeypatch.setattr(memorybank, "SUMMARY_INTERVAL", 1)

    class Stub:
        def __init__(self):
            self.prompts = []

        async def complete(self, system, user, **kwargs):
            self.prompts.append(user)
            return "A summary."

    stub = Stub()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    cursors.SUMMARY.anchor_at(adventure, ids["nodes"]["A3"])
    db.commit()

    asyncio.run(memorybank._update_story_summary(adventure, settings, db))

    [prompt] = stub.prompts
    assert "on B" in prompt and "on C" in prompt
    assert "on A's own continuation" not in prompt
    assert "on the shared trunk" not in prompt  # behind the mark
    # Caught up to the end of the story. Until SP4 that was C6: the newest
    # action was held back because retrying it rewrote the row underneath the
    # mark. A retry writes a sibling now, and the withdrawal that follows takes
    # the mark back with it, so there is nothing to hold back.
    assert cursors.SUMMARY.stored(adventure) == (ids["c"], 7)


def test_a_block_is_summarized_from_the_path_and_hung_off_its_last_node(
    forked, monkeypatch
):
    db, adventure, settings, ids = forked
    monkeypatch.setattr(memorybank, "MEMORY_START", 0)
    monkeypatch.setattr(memorybank, "MEMORY_INTERVAL", 4)

    class Stub:
        def __init__(self):
            self.excerpts = []

        async def complete(self, system, user, **kwargs):
            self.excerpts.append(user)
            return f"Memory {len(self.excerpts)}."

    stub = Stub()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    asyncio.run(memorybank._create_due_memories(adventure, settings, db))

    # Two blocks of four from a path of eight, and since SP4 nothing is held
    # back, so both form in one pass.
    first, second = stub.excerpts
    assert "A5" not in first + second, "a sibling's narration reached the summarizer"
    assert ["A0", "A1", "A2", "A3"] == [line for line in first.split() if line[0] in "ABC"]
    assert ["B4", "B5", "C6", "C7"] == [line for line in second.split() if line[0] in "ABC"]
    made = db.query(models.Memory).filter_by(text="Memory 1.").one()
    assert (made.branch_id, made.depth) == (ids["a"], 3)
    # The mark ends up on the node the second block attaches to, which is the tip.
    assert cursors.MEMORY.stored(adventure) == (ids["c"], 7)


# ------------------------------------------------------ the cost of forking

@pytest.fixture()
def deeply_forked():
    """A story forked twenty times, with a memory every six actions. This
    is the density the post-turn pass actually produces."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    user = models.User(is_guest=False, email="deepmem@example.com")
    db.add(user)
    db.flush()
    db.add(models.Settings(
        user_id=user.id, api_key="enc:dummy", model="m",
        embedding_model="text-embedding-3-small",
        # Every candidate is injected, so the measurement covers fetching the
        # texts too and not only ranking them.
        memory_top_k=50,
    ))

    def story(title, forks):
        adventure = models.Adventure(
            user_id=user.id, title=title, script_state={}, memory_bank_enabled=True,
        )
        db.add(adventure)
        db.flush()
        branch = make_branch(db, adventure)
        depth = 0
        nodes = []
        for _ in range(4):
            nodes.append(add_node(db, adventure, branch, depth, "n"))
            depth += 1
        for _ in range(forks):
            branch = make_branch(db, adventure, parent=branch, fork_depth=depth - 1)
            for _ in range(2):
                nodes.append(add_node(db, adventure, branch, depth, "n"))
                depth += 1
        if forks:
            branch = make_branch(db, adventure, parent=branch, fork_depth=depth - 1)
        for _ in range(84 - depth):
            nodes.append(add_node(db, adventure, branch, depth, "n"))
            depth += 1
        db.flush()
        for node in nodes[5::6]:  # one memory per six actions, as the pass makes them
            add_memory(db, adventure, f"memory at {node.depth}", node)
        adventure.head_branch_id = branch.id
        adventure.head_depth = depth - 1
        return adventure

    forked_story = story("Forked", 20)
    flat_story = story("Flat", 0)
    db.commit()
    try:
        yield db, flat_story, forked_story
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_retrieving_from_a_deep_fork_costs_what_a_flat_story_costs(deeply_forked):
    """The bet from the module docstring, measured in bytes. Retrieval
    names all twenty-two branches instead of one, but it fetches only an id
    and a flag per memory, and both stories return the same fourteen
    memories. The clause is where the difference shows up, and the clause
    is not what crosses the wire."""
    db, flat_story, forked_story = deeply_forked
    settings = db.query(models.Settings).one()
    flat_id, forked_id = flat_story.id, forked_story.id
    db.commit()
    db.expire_all()

    meter = dbmeter.Meter()
    meter.attach(engine)
    try:
        with meter.scope("flat"):
            assert len(retrieved(db.get(models.Adventure, flat_id), settings)) == 14
        flat_bytes = meter.scopes[-1].total.fetched
        with meter.scope("forked"):
            assert len(retrieved(db.get(models.Adventure, forked_id), settings)) == 14
        forked_bytes = meter.scopes[-1].total.fetched
    finally:
        meter.detach()

    # Measured 2026-08-18: 1,807 B against 1,823 B. Both figures cover the
    # same fourteen rows, named through twenty-two branch terms instead of one.
    assert flat_bytes > 0, "the meter saw nothing; it is measuring the wrong connection"
    assert forked_bytes < flat_bytes * 1.5, (
        f"retrieval on a 20-fork story cost {forked_bytes:,} B against the "
        f"{flat_bytes:,} B a flat story of the same length cost"
    )


# ------------------------------------------------------- the opening node

def test_a_typed_memory_on_the_opening_node_survives_that_node_going(forked):
    """The one exception where a node and its memories are not withdrawn
    together.

    A memory anchored to a node is withdrawn with the node. This is the
    rule, and it is deliberate: the memory described that turn, and the
    turn is leaving. But migration 62 parked every memory written before
    memories had coordinates on depth 0, the only landing spot visible from
    every branch. As a result, the opening node carries a whole bank of
    memories it never produced. Withdrawing it would delete all of those
    memories at once, for every adventure that predates the tree.

    A memory with no `source_start` covers no stretch of story, so nothing
    about it can go stale. It stays.
    """
    db, adventure, settings, ids = forked
    typed = models.Memory(
        adventure_id=adventure.id, text="Kira is the innkeeper's daughter",
        source_start=None, source_end=None,
    )
    typed.branch_id, typed.depth = ids["a"], 0
    db.add(typed)
    db.commit()
    typed_id = typed.id

    withdrawn = memorybank.forget_node(db, adventure, ids["nodes"]["A0"])
    db.commit()

    assert withdrawn == 0
    assert db.get(models.Memory, typed_id) is not None


def test_a_summary_of_the_opening_node_is_still_withdrawn(forked):
    """The exception is about memories that describe nothing, not about depth 0.

    A summary that genuinely ends on the opening node describes text that
    is being removed, so the summary is removed too. Otherwise the root
    would collect exactly the dangling rows that `forget_node` replaced
    `prune_dangling_memories` to prevent.
    """
    db, adventure, settings, ids = forked
    derived = add_memory(db, adventure, "the opening, summarised", ids["nodes"]["A0"])
    derived_id = derived.id

    withdrawn = memorybank.forget_node(db, adventure, ids["nodes"]["A0"])
    db.commit()

    assert withdrawn == 1
    assert db.get(models.Memory, derived_id) is None
