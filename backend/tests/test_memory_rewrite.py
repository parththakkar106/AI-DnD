"""Phase 18b: rewriting memories an older prompt wrote.

The prompt change only reaches memories written after it. A bank filled before
it keeps its unnamed, second-person entries and injects them into every turn
from then on, so there has to be a way to run the new prompt back over them.

Two halves, and the first is the one that can be wrong quietly:

* `memorybank.source_block` reads a memory's block back out of the story.
  Nothing in the app has ever had to do that. It has to read on the branch the
  memory was written on rather than the one the adventure is playing now, skip
  the sibling attempts at a retried turn, and cope with a memory whose actions
  have since been deleted.
* `tools/rewrite_memories` replaces the text and clears the vector, leaves a
  hand-written memory alone, and writes nothing at all without `--write`.

    python -m pytest tests/test_memory_rewrite.py -v
"""
import argparse
import asyncio

import pytest

from app import memorybank, models, tree
from app.context import lineage
from app.database import Base, SessionLocal, engine
from tools import rewrite_memories


class StubSummarizer:
    """Returns a numbered memory, and records what it was asked.

    The default text is what the new prompt asks for and the `OLD` text is what
    the old one produced, so an assertion says which prompt wrote a memory
    rather than counting calls.
    """

    OLD = "You entered the crypt {n}."

    def __init__(self, template="Kaelen entered the crypt {n}.", error=None):
        self.calls: list[tuple[str, str]] = []
        self.template = template
        self.error = error

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.template.format(n=len(self.calls))


class StubEmbedder:
    def __init__(self):
        self.texts: list[str] = []

    async def embed(self, texts):
        self.texts.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def make_adventure(db, *, actions=13, key="sk-test-key", email="rewrite@example.com"):
    """An adventure whose bank was written by the old prompt.

    Thirteen actions, not twelve: two blocks of six, plus the one action that
    settles the second of them. A block is not summarized while it ends on the
    newest action. See `memorybank.SETTLE_SLACK`.
    """
    user = models.User(is_guest=False, email=email)
    db.add(user)
    db.flush()
    # A key with no `enc:` prefix is stored plaintext and read back as-is; see
    # `security.decrypt_secret`. That keeps the fixture off the crypto path.
    db.add(models.Settings(user_id=user.id, api_key=key, model="test-model",
                           embedding_model="text-embedding-3-small"))
    adventure = models.Adventure(
        user_id=user.id, title="Camp", script_state={}, auto_summarize=True,
        memory="The player and Gwen are raiding a bandit camp.",
        persona_name="Kaelen", persona_pronouns="he/him",
        persona_desc="A half-elf ranger.",
    )
    db.add(adventure)
    db.flush()
    db.add(models.StoryCard(adventure_id=adventure.id, name="Gwen",
                            keys="Gwen, her", type="character",
                            entry="A loyal ranger and the player's ally."))
    for i in range(actions):
        db.add(models.Action(adventure_id=adventure.id,
                             type="ai" if i % 2 else "do",
                             text=f"You walk on. Action {i}."))
    db.commit()
    db.refresh(adventure)
    return adventure


def fill_bank(db, adventure, *, count=2):
    """Runs the real pass, so the memories carry the coordinates the real ones
    carry rather than coordinates this file made up."""
    stub = StubSummarizer(template=StubSummarizer.OLD)
    original = memorybank.summary_provider
    memorybank.summary_provider = lambda s: stub
    try:
        settings = (db.query(models.Settings)
                    .filter(models.Settings.user_id == adventure.user_id).one())
        asyncio.run(memorybank._create_due_memories(adventure, settings, db))
    finally:
        memorybank.summary_provider = original
    memories = (db.query(models.Memory)
                .filter(models.Memory.adventure_id == adventure.id)
                .order_by(models.Memory.id).all())
    assert len(memories) == count, f"expected {count} memories, got {len(memories)}"
    for memory in memories:
        memorybank.set_vector(memory, [1.0, 0.0, 0.0])
    db.commit()
    return memories


def options(**overrides):
    args = dict(write=False, adventure=None, email=None, limit=None,
                include_forgotten=False, embed=False, endpoint=None,
                model=None, api_key=None)
    args.update(overrides)
    return argparse.Namespace(**args)


def run_tool(args) -> int:
    return asyncio.run(rewrite_memories.main(args))


# ------------------------------------------------------ reading the block back

def test_the_block_is_the_actions_the_memory_covers(db):
    adventure = make_adventure(db)
    first, second = fill_bank(db, adventure)
    block = memorybank.source_block(db, first)
    assert [a.depth for a in block] == list(
        range(first.source_start, first.source_end + 1))
    assert len(block) == memorybank.MEMORY_INTERVAL
    assert [a.text for a in block] == [f"You walk on. Action {i}." for i in range(6)]
    assert [a.text for a in memorybank.source_block(db, second)] == [
        f"You walk on. Action {i}." for i in range(6, 12)]


def test_a_hand_written_memory_has_no_block(db):
    """It summarizes no actions, so there is nothing to rewrite it from — and
    the player may have typed it."""
    adventure = make_adventure(db)
    memory = models.Memory(adventure_id=adventure.id, text="Kaelen owes a debt.")
    tree.place_memory(db, adventure, memory)
    db.add(memory)
    db.commit()
    assert memorybank.source_block(db, memory) == []


def test_a_deleted_action_shortens_the_block(db):
    """A memory whose block is now partial still describes what remains."""
    adventure = make_adventure(db)
    first, _ = fill_bank(db, adventure)
    db.delete(memorybank.source_block(db, first)[2])
    db.commit()
    block = memorybank.source_block(db, first)
    assert len(block) == memorybank.MEMORY_INTERVAL - 1
    assert "Action 2." not in [a.text for a in block]


def test_a_retried_turn_contributes_only_its_live_attempt(db):
    """Sibling attempts share a coordinate. The block holds the one the story
    used, not both."""
    adventure = make_adventure(db)
    first, _ = fill_bank(db, adventure)
    kept = memorybank.source_block(db, first)[3]
    db.add(models.Action(adventure_id=adventure.id, branch_id=kept.branch_id,
                         depth=kept.depth, live=False, type=kept.type,
                         text="A discarded attempt."))
    db.commit()
    texts = [a.text for a in memorybank.source_block(db, first)]
    assert "A discarded attempt." not in texts
    assert len(texts) == memorybank.MEMORY_INTERVAL


def test_the_block_is_read_on_the_branch_the_memory_was_written_on(db):
    """The adventure has moved to a fork since. A memory on the trunk still
    reads back, and one left on the abandoned continuation reads that
    continuation rather than the branch now being played."""
    adventure = make_adventure(db)
    trunk_memory, _ = fill_bank(db, adventure)
    trunk = tree.head_branch(db, adventure)

    fork = models.Branch(adventure_id=adventure.id, parent_branch_id=trunk.id,
                         fork_depth=5, lineage=[])
    db.add(fork)
    db.flush()
    fork.lineage = [[fork.id, None], [trunk.id, 5]]
    for depth in (6, 7, 8, 9, 10, 11):
        db.add(models.Action(adventure_id=adventure.id, branch_id=fork.id,
                             depth=depth, type="do", text=f"Fork action {depth}."))
    db.flush()
    tip = (db.query(models.Action)
           .filter(models.Action.branch_id == fork.id)
           .order_by(models.Action.depth.desc()).first())
    fork_memory = models.Memory(adventure_id=adventure.id, text="On the fork.",
                                source_start=6, source_end=11)
    tree.attach_memory(fork_memory, tip)
    db.add(fork_memory)
    adventure.head_branch_id = fork.id
    adventure.head_depth = 11
    db.commit()

    # The trunk memory predates the fork and is inherited, so it reads the same
    # actions from either branch.
    assert [a.text for a in memorybank.source_block(db, trunk_memory)] == [
        f"You walk on. Action {i}." for i in range(6)]
    # The abandoned continuation's memory covers depths 6..11 on the trunk. The
    # fork covers the same depths with different actions, and the head is on the
    # fork, so a read that used the adventure's path would return the fork's.
    abandoned = (db.query(models.Memory)
                 .filter(models.Memory.branch_id == trunk.id,
                         models.Memory.source_start == 6).one())
    assert [a.text for a in memorybank.source_block(db, abandoned)] == [
        f"You walk on. Action {i}." for i in range(6, 12)]
    assert [a.text for a in memorybank.source_block(db, fork_memory)] == [
        f"Fork action {i}." for i in range(6, 12)]


def test_the_rewrite_prompt_is_the_one_the_app_sends(db):
    """`summarize_block` is the app's own prompt assembly. A rewrite that built
    its own would be written by a prompt that never shipped."""
    adventure = make_adventure(db)
    first, _ = fill_bank(db, adventure)
    stub = StubSummarizer()
    asyncio.run(memorybank.summarize_block(
        adventure, stub, memorybank.source_block(db, first)))
    system, user = stub.calls[0]
    assert "third person" in system
    assert user.index("Cast:") < user.index("Story excerpt:")
    assert "Kaelen (he/him) — the protagonist" in user


# --------------------------------------------------------------------- the tool

def test_the_database_line_carries_no_password():
    """The report names the database it is about to rewrite. That line ends up
    in a console, a screenshot or a pasted bug report."""
    shown = rewrite_memories.safe_dsn(
        "postgresql://parth:hunter2@ep-cool-frost.us-east-1.aws.neon.tech/aidnd"
        "?sslmode=require")
    assert "hunter2" not in shown
    assert "sslmode" not in shown  # a password can be passed there too
    assert shown == ("postgresql://parth@ep-cool-frost.us-east-1.aws.neon.tech"
                     "/aidnd")


def test_an_unparseable_database_url_shows_nothing_at_all():
    assert rewrite_memories.safe_dsn("not-a-url") == "(configured)"



def test_without_write_nothing_changes(db, monkeypatch):
    adventure = make_adventure(db)
    first, _ = fill_bank(db, adventure)
    stub = StubSummarizer()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    assert run_tool(options()) == 0
    db.expire_all()
    assert stub.calls == [], "a dry run must not call the model"
    assert first.text == "You entered the crypt 1."
    assert first.embedded is True


def test_write_replaces_the_text_and_clears_the_vector(db, monkeypatch):
    adventure = make_adventure(db)
    first, second = fill_bank(db, adventure)
    stub = StubSummarizer()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    assert run_tool(options(write=True)) == 0
    db.expire_all()
    assert [first.text, second.text] == ["Kaelen entered the crypt 1.", "Kaelen entered the crypt 2."]
    # The stored vector describes wording that no longer exists, so the memory
    # leaves the ranked bank until something embeds the new text.
    assert (first.embedded, first.embedding_blob) == (False, None)


def test_embed_puts_the_rewritten_memories_back_in_the_bank(db, monkeypatch):
    adventure = make_adventure(db)
    first, _ = fill_bank(db, adventure)
    embedder = StubEmbedder()
    summarizer = StubSummarizer()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: summarizer)
    monkeypatch.setattr(memorybank, "embedding_provider", lambda s: embedder)
    assert run_tool(options(write=True, embed=True)) == 0
    db.expire_all()
    assert embedder.texts == ["Kaelen entered the crypt 1.", "Kaelen entered the crypt 2."]
    assert first.embedded is True


def test_a_hand_written_memory_is_left_alone(db, monkeypatch):
    adventure = make_adventure(db)
    fill_bank(db, adventure)
    typed = models.Memory(adventure_id=adventure.id, text="Kaelen owes a debt.")
    tree.place_memory(db, adventure, typed)
    db.add(typed)
    db.commit()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: StubSummarizer())
    assert run_tool(options(write=True)) == 0
    db.expire_all()
    assert typed.text == "Kaelen owes a debt."


def test_an_owner_with_no_api_key_is_skipped(db, monkeypatch):
    """Summarization spends the user's own key and never the shared demo key."""
    adventure = make_adventure(db, key="")
    first, _ = fill_bank(db, adventure)
    stub = StubSummarizer()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: stub)
    assert run_tool(options(write=True)) == 0
    db.expire_all()
    assert stub.calls == []
    assert first.text == "You entered the crypt 1."


def test_an_api_key_on_the_command_line_covers_that_owner(db, monkeypatch):
    """The way to summarize for an owner who has no key of their own, and the
    way to point a run at the local Claude shim instead of a paid endpoint."""
    adventure = make_adventure(db, key="")
    first, _ = fill_bank(db, adventure)
    built: list[tuple] = []

    def build(*args, **kwargs):
        built.append(args)
        return StubSummarizer()

    monkeypatch.setattr("app.providers.OpenAICompatibleProvider", build)
    assert run_tool(options(write=True, api_key="sk-cli", model="sonnet",
                            endpoint="http://127.0.0.1:8787/v1")) == 0
    db.expire_all()
    assert first.text == "Kaelen entered the crypt 1."
    assert built[0][:3] == ("http://127.0.0.1:8787/v1", "sk-cli", "sonnet")


def test_limit_stops_early(db, monkeypatch):
    adventure = make_adventure(db)
    first, second = fill_bank(db, adventure)
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: StubSummarizer())
    assert run_tool(options(write=True, limit=1)) == 0
    db.expire_all()
    assert first.text == "Kaelen entered the crypt 1."
    assert second.text == "You entered the crypt 2."


def test_a_provider_error_leaves_the_old_text_and_reports_it(db, monkeypatch):
    from app.providers import ProviderError
    adventure = make_adventure(db)
    first, _ = fill_bank(db, adventure)
    monkeypatch.setattr(memorybank, "summary_provider",
                        lambda s: StubSummarizer(error=ProviderError("nope")))
    assert run_tool(options(write=True)) == 1
    db.expire_all()
    assert first.text == "You entered the crypt 1."
    assert first.embedded is True


def test_only_the_named_adventure_is_touched(db, monkeypatch):
    one = make_adventure(db)
    two = make_adventure(db, email="other@example.com")
    kept, _ = fill_bank(db, one)
    changed, _ = fill_bank(db, two)
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: StubSummarizer())
    assert run_tool(options(write=True, adventure=[two.id])) == 0
    db.expire_all()
    assert kept.text == "You entered the crypt 1."
    assert changed.text == "Kaelen entered the crypt 1."


def test_only_the_named_account_is_touched(db, monkeypatch):
    """The hosted database holds other people's stories, and each adventure is
    summarized with its owner's key."""
    mine = make_adventure(db, email="mine@example.com")
    theirs = make_adventure(db, email="theirs@example.com")
    kept, _ = fill_bank(db, theirs)
    changed, _ = fill_bank(db, mine)
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: StubSummarizer())
    assert run_tool(options(write=True, email=["MINE@example.com"])) == 0
    db.expire_all()
    assert kept.text == "You entered the crypt 1."
    assert changed.text == "Kaelen entered the crypt 1."


def test_an_unknown_email_is_an_error(db):
    make_adventure(db)
    assert run_tool(options(email=["nobody@example.com"])) == 2


def test_an_unknown_adventure_id_is_an_error(db):
    make_adventure(db)
    assert run_tool(options(adventure=[9999])) == 2


def test_an_evicted_memory_is_left_out_unless_asked_for(db, monkeypatch):
    adventure = make_adventure(db)
    first, second = fill_bank(db, adventure)
    second.forgotten = True
    db.commit()
    monkeypatch.setattr(memorybank, "summary_provider", lambda s: StubSummarizer())
    assert run_tool(options(write=True)) == 0
    db.expire_all()
    assert second.text == "You entered the crypt 2."
    assert run_tool(options(write=True, include_forgotten=True)) == 0
    db.expire_all()
    assert second.text.startswith("Kaelen entered")
