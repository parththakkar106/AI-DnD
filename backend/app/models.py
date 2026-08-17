from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    String, Table, Text, event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .compression import CompressedJSON
from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """Phase 8 — optional accounts.

    Three kinds of rows share this table:
      - the "local user" (email NULL, is_guest False): auto-created in
        single-user/local mode; owns everything a pre-Phase-8 DB had;
      - guests (email NULL, is_guest True): created on first visit in
        multi-user mode, identified only by their session cookie;
      - registered users (email set): a guest upgraded in place, so their
        data survives registration with no re-parenting.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Shared demo key usage (resets when the UTC date changes).
    demo_turns_used: Mapped[int] = mapped_column(Integer, default=0)
    demo_turns_date: Mapped[str] = mapped_column(String(10), default="")


scenario_scripts = Table(
    "scenario_scripts",
    Base.metadata,
    Column("scenario_id", ForeignKey("scenarios.id", ondelete="CASCADE"), primary_key=True),
    Column("script_id", ForeignKey("scripts.id", ondelete="CASCADE"), primary_key=True),
)


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL owner + is_public = seeded demo content, readable by everyone.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(200), default="Untitled Scenario")
    description: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    # Plot components (AI Dungeon terminology; `memory` == Plot Essentials)
    memory: Mapped[str] = mapped_column(Text, default="")
    authors_note: Mapped[str] = mapped_column(Text, default="")
    ai_instructions: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(500), default="")
    # Cover art. Either an external "https://…" URL or an inline
    # "data:image/…;base64,…" URI (the editor downscales uploads before storing
    # one). Empty means the UI falls back to an emoji sigil or generated art.
    # Kept in the row rather than on disk because Render's free tier has no
    # persistent volume, and it makes export bundles self-contained.
    image: Mapped[str] = mapped_column(Text, default="")
    # A single emoji or glyph used when there's no `image` — cheap art for
    # scenarios nobody wants to find a picture for. Separate from `image`
    # because it's a character, not a locator: no fetch, no cache, no bytes.
    icon: Mapped[str] = mapped_column(String(16), default="")
    # Phase 12: RPG world-state template — stat definitions (bands, rules) and
    # milestones. NULL/empty means this scenario has no RPG layer.
    stat_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    story_cards: Mapped[list["StoryCard"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    adventures: Mapped[list["Adventure"]] = relationship(back_populates="scenario")
    scripts: Mapped[list["Script"]] = relationship(secondary=scenario_scripts)


class Adventure(Base):
    __tablename__ = "adventures"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    scenario_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), default="Untitled Adventure")
    memory: Mapped[str] = mapped_column(Text, default="")
    authors_note: Mapped[str] = mapped_column(Text, default="")
    ai_instructions: Mapped[str] = mapped_column(Text, default="")
    story_summary: Mapped[str] = mapped_column(Text, default="")
    script_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # Phase 12: live RPG world state (world/player/npc stats + milestones),
    # instantiated from the scenario's stat_schema. Empty when there's no RPG layer.
    world_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # The ${Placeholder} answers collected when this adventure was started, kept
    # so "Update from scenario" can re-fill freshly copied scenario text with the
    # same values. NULL for adventures created before this column existed.
    placeholders: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Phase 6: opt-in per adventure (extra AI calls)
    auto_summarize: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_bank_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # LEGACY (Phase 6): how many actions had been folded into memories / the
    # story summary, as a *position* in the story. Unread since SP3, and
    # unwritten except by a v1 import which is handed one; kept for one release
    # so a rollback resumes from a real number, and dropped in SP8 beside
    # `actions.index`. The live mark is the anchor pair below.
    memory_cursor: Mapped[int] = mapped_column(Integer, default=0)
    summary_cursor: Mapped[int] = mapped_column(Integer, default=0)
    # Phase 14, SP3: the same two marks as nodes — (branch, depth) of the last
    # action each pass covered. A position slides when an action in front of it
    # is deleted and silently starts covering one it has never read; a depth
    # does not move, because it is a coordinate along a path rather than an
    # offset into a list. NO_DEPTH (-1) is "nothing covered yet", so the first
    # block needs no special case. Plain integers, not foreign keys, for the
    # same reason `head_branch_id` below is one. See `context/cursors.py`.
    memory_cursor_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_cursor_depth: Mapped[int] = mapped_column(Integer, default=-1)
    summary_cursor_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_cursor_depth: Mapped[int] = mapped_column(Integer, default=-1)
    # Phase 14: where the story is being played — which branch, and the depth of
    # its newest node. Deliberately NOT a ForeignKey: branches.adventure_id
    # already points this way, and a second constraint back would make the two
    # tables a cycle that create_all cannot order (the fix for that is
    # use_alter, which SQLite has no ALTER for). It is a cache of a pointer, and
    # `tree.head_branch` treats a head naming a branch that no longer exists as
    # a bug to recover from rather than a state to honour.
    head_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The depth of the tip, so the next node is always head_depth + 1.
    # NO_DEPTH (-1) for an adventure with no actions yet.
    head_depth: Mapped[int] = mapped_column(Integer, default=-1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    scenario: Mapped[Scenario | None] = relationship(back_populates="adventures")
    story_cards: Mapped[list["StoryCard"]] = relationship(
        back_populates="adventure", cascade="all, delete-orphan"
    )
    # Every action of the adventure — that is, every *branch's*. Not the story
    # being played, and re-ordering it by depth would not make it one: the
    # collection is the tree, and a path is a selection out of it. Anything
    # showing a reader a story goes through `context.history`, which goes
    # through the branch clause. What is left here is ownership and the
    # delete-orphan cascade, which are facts about the adventure.
    actions: Mapped[list["Action"]] = relationship(
        back_populates="adventure",
        cascade="all, delete-orphan",
        order_by="Action.index",
    )
    scripts: Mapped[list["AdventureScript"]] = relationship(
        back_populates="adventure",
        cascade="all, delete-orphan",
        order_by="AdventureScript.position",
    )
    memories: Mapped[list["Memory"]] = relationship(
        back_populates="adventure",
        cascade="all, delete-orphan",
        order_by="Memory.id",
    )


class Branch(Base):
    """Phase 14 — one path through an adventure's story tree.

    A branch does not own a copy of the story: it holds the nodes played on it
    and *borrows* everything before its fork point from its ancestors. Reading
    branch C means reading C's nodes, plus B's up to where C left it, plus A's
    up to where B left it — which is what `lineage` spells out, so a read is an
    OR-clause per entry instead of a walk up parent pointers.

    Until forking ships there is exactly one root branch per adventure and
    every node hangs off it. That is not a half-migrated state: a linear story
    *is* a tree with one branch, which is why writing these columns changes
    nothing anyone can observe.

    No ORM relationships on purpose. `actions.branch_id` and `memories
    .branch_id` carry ON DELETE CASCADE, so the database removes a deleted
    branch's nodes; a relationship would have SQLAlchemy load them all to do
    the same thing, and loading every action of a branch is the exact cost the
    windowed reads exist to avoid.
    """

    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    # NULL on a root branch.
    parent_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    # The depth this branch left its parent at, stored when the fork happens and
    # never inferred afterwards. Inferring it from where two branches' nodes
    # first differ would be a guess about how the story was played — and a wrong
    # one as soon as an attempt happens to repeat its parent's text.
    fork_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The ancestry, newest first: [[branch_id, max_depth], ...] where max_depth
    # is NULL for "to the tip" and otherwise the fork_depth of the branch
    # beneath it, inclusive. Computed once at fork from the parent's lineage
    # plus one entry, so no read ever reconstructs it.
    lineage: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Memory(Base):
    """Phase 6: an auto-summarized (or hand-written) fact about the adventure.

    The vector lives in `embedding_blob` as packed float32 (see vectors.py).
    NULL until embedded, which also marks it for backfill when an embedding
    model becomes available.

    Cosine ranking happens in Python, which means the vectors cross the wire.
    The original comment here sized that by count — "fine at a few hundred" —
    and it was wrong by the only measure that mattered: a few hundred JSON
    vectors is ten megabytes, fetched fresh every turn. Weigh new columns in
    bytes.
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text, default="")
    # The vector, little-endian float32. Deferred because it is wider than the
    # rest of the row put together and exactly one code path wants it: anything
    # bulk-loading memories (the Memories drawer, eviction, the embed queue)
    # must project the columns it needs rather than load whole entities.
    embedding_blob: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, deferred=True
    )
    # The stretch of story this memory summarizes, as depths on `branch_id`
    # (null for a hand-written memory, which summarizes nothing). Written as
    # `Action.index` values before SP3, which held the same numbers.
    # `source_end` is the depth of the node the memory hangs off, mirrored into
    # `depth` below; `source_start` is where it began, which is where the
    # summarizer has to resume from if the memory is ever withdrawn.
    source_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 14: the node that produced this memory — the last action it
    # summarises. Anything derived attaches to the node it came from, which is
    # what makes a fork free: a shared ancestor's memories are shared
    # automatically, and a memory covering a stretch of branch B is invisible
    # from any path that does not go through B.
    #
    # `depth` is NULL for a hand-written memory, which no node produced; that
    # reads as "belongs to the adventure, not to a path".
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Whether embedding_blob is set. Maintained on write by memorybank
    # .set_vector, for the same reason actions.variant_count exists beside
    # actions.variants: every reader wants the one-bit answer and none of them
    # should have to fetch six kilobytes of vector to get it.
    embedded: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    forgotten: Mapped[bool] = mapped_column(Boolean, default=False)  # evicted, kept for UI
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    adventure: Mapped[Adventure] = relationship(back_populates="memories")


class StoryCard(Base):
    """Owned by either a scenario or an adventure (exactly one set)."""

    __tablename__ = "story_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=True
    )
    adventure_id: Mapped[int | None] = mapped_column(
        ForeignKey("adventures.id", ondelete="CASCADE"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    keys: Mapped[str] = mapped_column(Text, default="")  # comma-separated triggers
    entry: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    # Adventure copies only: which piece of the scenario this card came from —
    # "card:<scenario_card_id>" or "npc:<npc_key>". "Update from scenario"
    # refreshes/removes exactly these; NULL means player-authored (left alone),
    # or a copy predating the column (matched by name, then adopted).
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    scenario: Mapped[Scenario | None] = relationship(back_populates="story_cards")
    adventure: Mapped[Adventure | None] = relationship(back_populates="story_cards")


class Action(Base):
    __tablename__ = "actions"
    # Phase 14: every read of a story is "this branch up to this depth, or that
    # branch up to that depth, ...", so (branch_id, depth) is the shape every
    # one of those clauses wants an index on.
    __table_args__ = (Index("ix_actions_branch_depth", "branch_id", "depth"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    # Phase 14: the node's place in the tree. `depth` is a position along *a*
    # path, not a global turn number — A4 and B4 are alternatives, not
    # duplicates — and it replaces `index` as the ordering key.
    #
    # Nullable because ALTER TABLE cannot add a NOT NULL column with no
    # default and there is no sensible default for "which branch": the
    # migration fills them, `tree.place_action` fills them for new nodes, and
    # from SP2 on a NULL branch_id is a row no read can see. Legacy `index`
    # stays beside them, unread, until the tree is proven live (SP8 drops it).
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 14, SP4: whether this node is the one the story tells at its
    # coordinate. Retry no longer rewrites a row — it writes a *sibling* at the
    # same (branch, depth), so a coordinate can hold several attempts and
    # exactly one of them is on the path. `lineage.Path.clause` is the only
    # place that reads this, for the same reason it is the only place that
    # knows about branches: an attempt leaking into a read is a story quietly
    # telling itself twice.
    #
    # A node with no siblings is live, which is why the default is True and why
    # every pre-SP4 row is correct without being visited.
    live: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20))  # start|do|say|story|continue|ai
    text: Mapped[str] = mapped_column(Text, default="")
    # Reasoning-model "thinking" that preceded the text (AI actions only).
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The full assembled prompt for this turn, for the Insights viewer. By far
    # the biggest column in the database — 163 KB a row averaged over
    # production and 232 KB on the longest adventure, 89% of everything stored
    # — and needed by exactly one endpoint, one action at a time.
    #
    # Two separate defences, because it is expensive in two separate ways.
    # `deferred=True` is the read defence: never loaded unless something
    # touches the attribute, so a page load pays nothing for it. Bulk readers
    # must NOT touch it; that is what `world_delta` below exists for.
    # CompressedJSON is the *storage* defence: this is the column that decides
    # when the free tier's 512 MB runs out. Still a dict either way — see
    # compression.py.
    context_snapshot: Mapped[dict | None] = mapped_column(
        CompressedJSON, nullable=True, deferred=True
    )
    # The small slice of the snapshot that IS needed in bulk: this turn's RPG
    # state changes, for the inline chips under an AI message (world_changes)
    # and for re-attaching the emit block when replaying history to the model.
    # Mirrors the active variant, same as text/reasoning/context_snapshot.
    world_delta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # LEGACY (SP4): Adventure.script_state / world_state as they were
    # immediately BEFORE this action's script hooks ran. Unwritten since SP4
    # and read by nothing — the *after* pair below replaced them, because a
    # sibling attempt needs its own outcome and a "before" picture is shared by
    # every attempt at the turn. Kept for one release so a rolled-back build
    # still finds a real snapshot on every row it wrote itself; SP8 drops them
    # beside `index` and `variants`.
    state_before: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    world_state_before: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    # Phase 14, SP4: the shared script scoreboard and the RPG world state as
    # they stood once this node had been played — *its* outcome, not its
    # starting position.
    #
    # Two things want this and neither can use a "before" picture. Switching
    # between siblings has to put back the state the chosen attempt produced,
    # and the attempts differ precisely in what they produced. And rolling back
    # to before a turn is "the state the node in front of it left behind",
    # which is one lookup on the path rather than a snapshot that has to be
    # taken from inside the turn being rolled back.
    #
    # NULL on rows written before SP4 that the migration could not derive one
    # for, and tolerated everywhere: a missing snapshot means "leave the live
    # state alone", never "reset it".
    #
    # Deferred: only ever read for the one node being switched to, undone or
    # retried past.
    state_after: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    world_state_after: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    # LEGACY (SP4): retry history as a JSON repeating group. Every attempt at
    # this turn is its own row now — see `live` above and `app/attempts.py` —
    # so nothing reads this. Kept until SP8 for the same reason `index` is, and
    # read exactly once more on the way out: migration 60 is what turns each
    # entry into the sibling row it should always have been.
    variants: Mapped[list | None] = mapped_column(JSON, nullable=True, deferred=True)
    # Where the row sits in its sibling group: `variant_index` is this
    # attempt's ordinal, oldest first, and `variant_count` is how many attempts
    # the group holds (0, not 1, when the turn was never retried — the pager
    # reads that as "nothing to page through").
    #
    # A cache of two facts about the group, maintained in one place
    # (`attempts.renumber`) for the same reason it used to be a cache of
    # `len(variants)`: a page response wants them for every row and must not
    # pay a query per turn to get them. SP7 replaces the pager with the branch
    # view and SP8 drops both columns.
    variant_count: Mapped[int] = mapped_column(Integer, default=0)
    variant_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    adventure: Mapped[Adventure] = relationship(back_populates="actions")

    @property
    def world_changes(self) -> list[dict]:
        """Compact per-turn RPG state changes (Phase 12), for the inline summary
        under an AI message. Labels are path-based (no schema needed):
        `npc.gwen.trust` -> "gwen trust".

        Reads `world_delta`, never `context_snapshot` — this runs for every
        action in a list response, and touching the deferred snapshot here
        would drag the whole prompt archive out of the database."""
        wd = self.world_delta if isinstance(self.world_delta, dict) else None
        if wd is None:
            return []
        applied = wd.get("applied") or []
        out: list[dict] = []
        for entry in applied:
            parts = str(entry.get("path", "")).split(".")
            section, name = parts[0], parts[-1]
            if section == "flags":
                out.append({"kind": "flag", "label": name, "on": bool(entry.get("new"))})
            elif section == "milestones":
                out.append({"kind": "milestone", "label": name})
            else:
                label = f"{parts[1]} {parts[2]}" if section == "npc" and len(parts) == 3 else name
                old, new = entry.get("old"), entry.get("new")
                delta = new - old if isinstance(old, (int, float)) and isinstance(new, (int, float)) else None
                out.append({"kind": "stat", "label": label, "delta": delta, "value": new})
        return out


class Script(Base):
    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), default="Untitled Script")
    description: Mapped[str] = mapped_column(Text, default="")
    library_js: Mapped[str] = mapped_column(Text, default="")
    input_js: Mapped[str] = mapped_column(Text, default="")
    context_js: Mapped[str] = mapped_column(Text, default="")
    output_js: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AdventureScript(Base):
    """A script copied into an adventure at creation, so library edits don't
    change running adventures unless the player explicitly re-syncs it from
    `source_script_id`. `state` lives on Adventure.script_state (one shared
    state per adventure, as in AI Dungeon)."""

    __tablename__ = "adventure_scripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    # The library Script this copy was made from, so it can be re-synced on
    # demand. NULL for legacy copies (predate this column) and demo-derived
    # ones whose source isn't owned by the player — those fall back to a
    # name match, or simply aren't syncable.
    source_script_id: Mapped[int | None] = mapped_column(
        ForeignKey("scripts.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    name: Mapped[str] = mapped_column(String(200), default="Untitled Script")
    description: Mapped[str] = mapped_column(Text, default="")
    library_js: Mapped[str] = mapped_column(Text, default="")
    input_js: Mapped[str] = mapped_column(Text, default="")
    context_js: Mapped[str] = mapped_column(Text, default="")
    output_js: Mapped[str] = mapped_column(Text, default="")

    adventure: Mapped[Adventure] = relationship(back_populates="scripts")


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Phase 8: one row per user (pre-Phase-8 DBs had a single id=1 row, which
    # the migration assigns to the local user).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    endpoint_url: Mapped[str] = mapped_column(String(500), default="http://localhost:11434/v1")
    # Fernet-encrypted at rest ("enc:..." — see security.py); use api_key_plain.
    api_key: Mapped[str] = mapped_column(String(500), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    api_mode: Mapped[str] = mapped_column(String(20), default="chat")  # chat|completion
    temperature: Mapped[float] = mapped_column(Float, default=0.8)
    # 800 leaves room for a full scene; 400 tended to truncate mid-paragraph
    # and left reasoning models with nothing after their thinking.
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=800)
    # Separate thinking budget for reasoning models (OpenRouter-style
    # `reasoning: {max_tokens}`); 0 = param not sent, -1 = reasoning explicitly
    # off (`reasoning: {effort: none}`). Added on top of
    # max_output_tokens so story output keeps its full budget.
    reasoning_max_tokens: Mapped[int] = mapped_column(Integer, default=0)
    context_token_budget: Mapped[int] = mapped_column(Integer, default=16384)
    narrator_prompt: Mapped[str] = mapped_column(
        Text,
        default=(
            "You are a masterful storyteller continuing an interactive adventure. "
            "Continue the story naturally in second person, staying consistent with "
            "everything established so far. Write vivid prose. Never speak for the "
            "player or break character. Do not conclude the story; always leave room "
            "for the player's next action."
        ),
    )
    stream: Mapped[bool] = mapped_column(Boolean, default=True)
    # Phase 6: auto-summarization + memory bank
    summary_model: Mapped[str] = mapped_column(String(200), default="")  # "" = main model
    embedding_model: Mapped[str] = mapped_column(String(200), default="")  # "" = bank disabled
    # Was 200. Lowered on retrieval-quality grounds first: ranking two hundred
    # memories to pick five means the five are chosen out of a lot of noise,
    # and older memories describe a story the player has moved on from. That it
    # also cuts what the bank costs to read is the smaller reason.
    memory_bank_capacity: Mapped[int] = mapped_column(Integer, default=80)
    memory_top_k: Mapped[int] = mapped_column(Integer, default=5)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def api_key_plain(self) -> str:
        from . import security  # local import: models is imported before security

        return security.decrypt_secret(self.api_key)


# Phase 14 — the floor under `tree.place_action`.
#
# From SP2 a read selects on (branch_id, depth): a node written without them is
# a node no page, no context build and no memory pass can see, and it fails by
# disappearing rather than by raising. The writers all place their nodes
# explicitly, but "all the writers remember" is a promise that has to hold for
# every fixture, script and test written from here on, so the session enforces
# it on the way to the database instead.
#
# Registered here rather than in tree.py so that importing the models is enough
# to arm it — the invariant belongs to the rows, not to the module that usually
# writes them. The import is deferred into the callback because tree.py imports
# this module.
@event.listens_for(Session, "before_flush")
def _place_new_nodes_on_the_tree(session, flush_context, instances):
    from . import tree

    tree.place_new_nodes(session)
