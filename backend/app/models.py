from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    String, Table, Text, UniqueConstraint, event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .compression import CompressedJSON
from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """Phase 8: optional accounts.

    Three kinds of row share this table:

    - The local user has a NULL email and `is_guest` set to False. Single-user
      mode creates this row automatically. It owns everything that a database
      from before Phase 8 contained.
    - Guests have a NULL email and `is_guest` set to True. Multi-user mode
      creates one on a visitor's first visit and identifies it only by the
      session cookie.
    - Registered users have an email. Registration upgrades a guest row in
      place, so the guest's data survives without being reassigned.
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
    # Cover art. The value is either an "https://" URL or an inline
    # "data:image/...;base64,..." URI. The editor downscales uploads before
    # storing them. An empty value tells the UI to fall back to an emoji sigil
    # or to generated art. The image is stored in the row rather than on disk,
    # because Render's free tier provides no persistent volume. Storing it here
    # also keeps export bundles self-contained.
    image: Mapped[str] = mapped_column(Text, default="")
    # A single emoji or glyph, used when `image` is empty. This is a separate
    # column because the value is a character rather than a location, so
    # nothing needs to fetch or cache it.
    icon: Mapped[str] = mapped_column(String(16), default="")
    # Phase 12: the RPG world-state template. It holds stat definitions, which
    # include bands and rules, and milestones. A NULL or empty value means the
    # scenario has no RPG layer.
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
    # Legacy columns from Phase 6. Each holds a count of the actions that were
    # folded into the memories or the story summary, expressed as a position in
    # the story. Nothing has read them since SP3, and nothing writes them except
    # a v1 import. They remain for one release so that a rollback resumes from a
    # real number. SP8 drops them along with `actions.index`. The columns below
    # hold the marks that this code actually uses.
    memory_cursor: Mapped[int] = mapped_column(Integer, default=0)
    summary_cursor: Mapped[int] = mapped_column(Integer, default=0)
    # Phase 14, SP3: the same two marks expressed as coordinates. Each pair
    # holds the branch and depth of the last action that pass covered. A
    # position moves when an action in front of it is deleted, so the mark
    # silently starts covering an action it never read. A depth is a coordinate
    # along a path, so deleting an action does not move it. NO_DEPTH, which is
    # -1, means that nothing is covered yet, so the first block needs no special
    # case. These are plain integers rather than foreign keys, for the reason
    # given on `head_branch_id` below. See `context/cursors.py`.
    memory_cursor_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_cursor_depth: Mapped[int] = mapped_column(Integer, default=-1)
    summary_cursor_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_cursor_depth: Mapped[int] = mapped_column(Integer, default=-1)
    # Phase 14: where the story is being played. `head_branch_id` names the
    # branch, and `head_depth` gives the depth of its newest node.
    #
    # `head_branch_id` is deliberately not a ForeignKey. `branches.adventure_id`
    # already points from branches to adventures, so a constraint in this
    # direction would make the two tables a cycle that `create_all` cannot
    # order. The usual fix is `use_alter`, which needs an ALTER statement that
    # SQLite does not provide. The column caches a pointer, and
    # `tree.head_branch` treats a head that names a missing branch as a bug to
    # recover from rather than a state to preserve.
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
    # Every action in the adventure, across all branches. This collection is
    # the tree, not the story being played. Ordering it by depth does not make
    # it a story, because a path is a selection out of the tree. Code that shows
    # a story to a reader goes through `context.history`, which applies the
    # branch clause. This relationship exists for ownership and for the
    # delete-orphan cascade.
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
    """Phase 14: one path through an adventure's story tree.

    A branch does not own a copy of the story. It holds the nodes played on it,
    and it inherits everything before its fork point from its ancestors. Reading
    branch C means reading C's nodes, then B's nodes up to the depth where C
    forked, then A's nodes up to the depth where B forked. The `lineage` column
    records that list, so a read becomes one OR clause per entry instead of a
    walk up parent pointers.

    Until forking ships, each adventure has one root branch and every node
    belongs to it. This is not a partly migrated state. A linear story is a tree
    with one branch, which is why writing these columns changes nothing that a
    reader can observe.

    This class defines no ORM relationships, by design. `actions.branch_id` and
    `memories.branch_id` both use ON DELETE CASCADE, so the database removes a
    deleted branch's nodes. A relationship would make SQLAlchemy load those rows
    first, and loading every action of a branch is what the windowed reads exist
    to avoid.
    """

    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    # NULL on a root branch.
    parent_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    # The depth at which this branch left its parent. The fork records this
    # value, and no code infers it later. Deriving it from the first depth where
    # two branches' nodes differ would produce a wrong answer whenever an
    # attempt repeats its parent's text.
    fork_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The ancestry, newest first, as [[branch_id, max_depth], ...]. A NULL
    # `max_depth` means the entry extends to the tip of that branch. Any other
    # value is the fork depth of the branch below it, inclusive. The fork
    # computes this list once from the parent's lineage plus one entry, so no
    # read has to reconstruct it.
    lineage: Mapped[list] = mapped_column(JSON, default=list)
    # The name the player gave this line of the story, or NULL if no one named
    # it. The column stores NULL rather than a generated name such as
    # "branch 4", because it records what the player chose rather than what the
    # app derived. A stored default would also become wrong as soon as an
    # earlier branch is deleted and the ordinals shift. The UI labels an unnamed
    # branch by its fork depth, which deleting a branch does not change.
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Memory(Base):
    """Phase 6: an auto-summarized (or hand-written) fact about the adventure.

    The vector lives in `embedding_blob` as packed float32 (see vectors.py).
    NULL until embedded, which also marks it for backfill when an embedding
    model becomes available.

    Cosine ranking runs in Python, so the vectors travel over the wire. Measure
    that cost in bytes rather than in rows. A few hundred vectors stored as JSON
    come to about 10 MB, fetched again on every turn. Size any new column by the
    bytes it adds, not by the number of rows.
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text, default="")
    # The vector, stored as little-endian float32. This column is deferred
    # because it is wider than the rest of the row combined and only one code
    # path reads it. Code that loads memories in bulk, such as the Memories
    # drawer, eviction, and the embed queue, must select the columns it needs
    # instead of loading whole entities.
    embedding_blob: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, deferred=True
    )
    # The stretch of story this memory summarizes, given as depths on
    # `branch_id`. Both are NULL for a hand-written memory, which summarizes no
    # actions. Before SP3 these columns held `Action.index` values, which were
    # the same numbers. `source_end` is the depth of the node the memory
    # attaches to, and `depth` below mirrors it. `source_start` is where the
    # stretch begins, which is where the summarizer resumes if the memory is
    # withdrawn.
    source_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 14: the node that produced this memory, meaning the last action the
    # memory summarizes. Derived data attaches to the node it came from, which
    # is what makes forking cheap. Memories on a shared ancestor are shared
    # automatically, and a memory that covers part of branch B is not visible
    # from any path that does not go through B.
    #
    # Every memory has a coordinate, including a hand-written one, which takes
    # the head as of the moment it was written (SP7). A NULL depth used to mean
    # that the memory belonged to the adventure rather than to a path. A fork
    # cannot cap a NULL, so such a memory followed the reader onto branches
    # whose story it did not describe.
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Whether `embedding_blob` is set. `memorybank.set_vector` keeps this
    # column current, for the same reason that `actions.variant_count` sits
    # beside `actions.variants`. Readers need only the yes-or-no answer, and
    # fetching six kilobytes of vector to get it is too expensive.
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
    # Set on adventure copies only. It records which piece of the scenario the
    # card came from, as either "card:<scenario_card_id>" or "npc:<npc_key>".
    # The "Update from scenario" action refreshes or removes exactly these
    # cards. A NULL value means the player wrote the card, so the update leaves
    # it alone, or that the copy predates this column, in which case the update
    # matches it by name and then sets this value.
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    scenario: Mapped[Scenario | None] = relationship(back_populates="story_cards")
    adventure: Mapped[Adventure | None] = relationship(back_populates="story_cards")


def _change_label(parts: list[str]) -> str:
    """Names a world-state path for the inline turn summary.

    `npc.gwen.trust` becomes "gwen trust". Every other shape uses its last
    segment, so `player.hp` becomes "hp".
    """
    if len(parts) == 3 and parts[0] == "npc":
        return f"{parts[1]} {parts[2]}"
    return parts[-1] if parts else ""


class Action(Base):
    __tablename__ = "actions"
    # Phase 14: every story read selects one branch up to one depth, then
    # another branch up to another depth, and so on. The pair (branch_id, depth)
    # is the index those clauses need.
    __table_args__ = (Index("ix_actions_branch_depth", "branch_id", "depth"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    # Phase 14: the node's place in the tree. `depth` is a position along one
    # path rather than a global turn number. Node A4 and node B4 are
    # alternatives, not duplicates. `depth` replaces `index` as the ordering
    # key.
    #
    # Both columns are nullable because ALTER TABLE cannot add a NOT NULL column
    # without a default, and no default makes sense for a branch. The migration
    # fills these columns for existing rows, and `tree.place_action` fills them
    # for new rows. From SP2 onward, a NULL `branch_id` marks a row that no read
    # can see. The legacy `index` column stays alongside, unread, until the tree
    # is proven in production. SP8 drops it.
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 14, SP9: the node that this node was played after. It names the take
    # that was live when this row was written, not whatever sits at depth - 1
    # now.
    #
    # This column answers one question: which takes belong to the same turn. A
    # coordinate cannot answer it. A take that is forked onto its own branch
    # leaves the coordinate that its siblings still occupy, so the pager would
    # show it as 1/1 next to their 1/3. Forking a branch does not change a
    # node's parent.
    #
    # Code reads this column only to group takes, using one indexed lookup
    # rather than a walk. Paths still resolve through `lineage`, which is why
    # adding this column required no change to any read of the story.
    #
    # The value is NULL on a root node, and on pre-SP9 rows that the migration
    # could not place. For those rows, `attempts.group` falls back to the
    # coordinate, which is how they were written.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("actions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Phase 14, SP4: whether this node is the one the story uses at its
    # coordinate. Retry no longer rewrites a row. It writes a sibling at the
    # same branch and depth, so one coordinate can hold several attempts while
    # exactly one of them is on the path. `lineage.Path.clause` is the only
    # place that reads this column, for the same reason it is the only place
    # that knows about branches. If a discarded attempt reaches a read, the page
    # renders the same turn twice.
    #
    # A node with no siblings is live, so the default is True and every pre-SP4
    # row is already correct. The migration does not need to visit them.
    live: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20))  # start|do|say|story|continue|ai
    text: Mapped[str] = mapped_column(Text, default="")
    # Reasoning-model "thinking" that preceded the text (AI actions only).
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The full assembled prompt for this turn, used by the Insights viewer.
    # This is the largest column in the database. It averages 163 KB per row in
    # production and 232 KB on the longest adventure, and it accounts for 89% of
    # everything stored. Only one endpoint reads it, one action at a time.
    #
    # The column is expensive in two ways, so it has two protections.
    # `deferred=True` protects reads, because SQLAlchemy loads the column only
    # when code touches the attribute. A page load therefore costs nothing. Code
    # that reads actions in bulk must not touch this attribute, which is why
    # `world_delta` below exists. `CompressedJSON` protects storage, because
    # this column determines when the free tier's 512 MB limit is reached. The
    # attribute behaves like a plain dict in both cases. See compression.py.
    context_snapshot: Mapped[dict | None] = mapped_column(
        CompressedJSON, nullable=True, deferred=True
    )
    # The small slice of the snapshot that IS needed in bulk: this turn's RPG
    # state changes, for the inline chips under an AI message (world_changes)
    # and for re-attaching the emit block when replaying history to the model.
    # Mirrors the active variant, same as text/reasoning/context_snapshot.
    world_delta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Legacy columns from before SP4. They hold `Adventure.script_state` and
    # `Adventure.world_state` as they were immediately before this action's
    # script hooks ran. Nothing has written or read them since SP4. The pair of
    # "after" columns below replaced them, because each sibling attempt needs
    # its own outcome and every attempt at a turn shares the same starting
    # state. These columns remain for one release so that a rolled-back build
    # still finds a real snapshot on the rows it wrote. SP8 drops them along
    # with `index` and `variants`.
    state_before: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    world_state_before: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    # Phase 14, SP4: the shared script state and the RPG world state as they
    # stood after this node was played. These columns record the node's outcome
    # rather than its starting position.
    #
    # Two operations need this outcome, and neither can use a snapshot taken
    # before the turn. Switching between siblings must restore the state that
    # the chosen attempt produced, and the attempts differ in exactly that.
    # Rolling back to before a turn means restoring the state that the preceding
    # node left behind, which is one lookup along the path.
    #
    # The value is NULL on pre-SP4 rows for which the migration could not derive
    # one. Every caller tolerates that. A missing snapshot means that the caller
    # leaves the live state unchanged. It never means reset the state.
    #
    # These columns are deferred, because code reads them only for the single
    # node being switched to, undone, or retried past.
    state_after: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    world_state_after: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, deferred=True
    )
    # A legacy column from before SP4. It holds the retry history as a repeating
    # group inside a JSON list. Every attempt at a turn is now its own row, as
    # described on `live` above and in `app/attempts.py`, so nothing reads this
    # column. It remains until SP8 for the same reason `index` does. Migration
    # 60 reads it once more, to turn each entry into a sibling row.
    variants: Mapped[list | None] = mapped_column(JSON, nullable=True, deferred=True)
    # Where the row sits in its sibling group. `variant_index` is this
    # attempt's ordinal, counting from the oldest. `variant_count` is the number
    # of attempts in the group. It is 0 rather than 1 when the turn was never
    # retried, which the pager treats as having nothing to page through.
    #
    # Both columns are caches, and `attempts.renumber` is the only place that
    # maintains them. The reason matches why `variant_count` once cached
    # `len(variants)`: a page response needs both numbers for every row and
    # cannot afford one query per turn. SP7 replaces the pager with the branch
    # view, and SP8 drops both columns.
    variant_count: Mapped[int] = mapped_column(Integer, default=0)
    variant_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    adventure: Mapped[Adventure] = relationship(back_populates="actions")

    @property
    def world_changes(self) -> list[dict]:
        """Compact per-turn RPG state changes (Phase 12), for the inline summary
        under an AI message. Labels are path-based (no schema needed):
        `npc.gwen.trust` -> "gwen trust".

        The summary reports refused changes as well as accepted ones. A stat the
        engine clamped carries `clamped`, and a stat it refused outright becomes
        a `rejected` entry carrying the reason. Reporting only the accepted
        changes made a clamp indistinguishable from a change that never
        happened: a value the model pushed past its ceiling came back as a
        delta of 0 and rendered as an ordinary chip, so a refused update read on
        screen as an applied one.

        Reads `world_delta`, never `context_snapshot`. This runs for every
        action in a list response, and touching the deferred snapshot here would
        drag the entire prompt archive out of the database."""
        wd = self.world_delta if isinstance(self.world_delta, dict) else None
        if wd is None:
            return []
        clamped_paths = {
            str(e.get("path", "")) for e in (wd.get("clamped") or []) if isinstance(e, dict)
        }
        out: list[dict] = []
        for entry in wd.get("applied") or []:
            path = str(entry.get("path", ""))
            parts = path.split(".")
            section, name = parts[0], parts[-1]
            if section == "flags":
                out.append({"kind": "flag", "label": name, "on": bool(entry.get("new"))})
            elif section == "milestones":
                out.append({"kind": "milestone", "label": name})
            else:
                old, new = entry.get("old"), entry.get("new")
                delta = new - old if isinstance(old, (int, float)) and isinstance(new, (int, float)) else None
                chip = {
                    "kind": "stat",
                    "label": _change_label(parts),
                    "delta": delta,
                    "value": new,
                    "clamped": path in clamped_paths,
                }
                # Carried only when the engine wrote one. It is empty for every
                # accepted change, and a key per chip per action is paid on
                # every page load.
                if entry.get("fix"):
                    chip["fix"] = str(entry["fix"])
                out.append(chip)
        for entry in wd.get("rejected") or []:
            if not isinstance(entry, dict):
                continue
            parts = str(entry.get("path", "")).split(".")
            chip = {
                "kind": "rejected",
                "label": _change_label(parts),
                "reason": str(entry.get("reason", "")),
            }
            if entry.get("fix"):
                chip["fix"] = str(entry["fix"])
            out.append(chip)
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
    # The library Script that this copy was made from, which lets the player
    # re-sync it on demand. The value is NULL for legacy copies that predate
    # this column, and for demo-derived copies whose source the player does not
    # own. Those copies fall back to matching by name, or cannot be synced.
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
    # Encrypted at rest with Fernet, which produces a value that starts with
    # "enc:". See security.py. To read the key, use `api_key_plain`.
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
    # This was 200. It was lowered mainly to improve retrieval quality. Ranking
    # 200 memories to choose 5 selects from a large amount of noise, and the
    # oldest memories describe a part of the story that the player has left
    # behind. Cheaper reads are a secondary benefit rather than the reason.
    memory_bank_capacity: Mapped[int] = mapped_column(Integer, default=80)
    memory_top_k: Mapped[int] = mapped_column(Integer, default=5)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def api_key_plain(self) -> str:
        from . import security  # local import: models is imported before security

        return security.decrypt_secret(self.api_key)


# ---------- Visit analytics (see analytics.py) ----------
# Two intentionally simple tables. Neither can hold text that a player wrote,
# and neither can be joined back to a `users` row, because the visitor column
# holds an HMAC and has no foreign key. When guest cleanup deletes an account,
# the history that account contributed remains intact and anonymous.


class AnalyticsDaily(Base):
    """One counter: how many times `label` happened within `metric` on `day`.

    The table stores a generic triple of metric, label, and hits rather than one
    column per statistic. Measuring something new therefore costs a constant
    rather than a migration. The only writer is an UPSERT that runs from a
    buffer. See `analytics.flush`.
    """

    __tablename__ = "analytics_daily"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD, UTC
    metric: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(80), default="")
    hits: Mapped[int] = mapped_column(Integer, default=0)

    # The upsert target: one row per bucket per day, created or incremented.
    __table_args__ = (
        UniqueConstraint("day", "metric", "label", name="uq_analytics_daily_bucket"),
    )


class AnalyticsVisitorDay(Base):
    """One visitor, one day, and which funnel steps they reached on it.

    This table exists so that the funnel counts people rather than clicks. A
    player who starts six adventures counts as one person who started an
    adventure. `is_new` is set when the visitor has no earlier row, which is why
    the visitor column also has an index of its own.
    """

    __tablename__ = "analytics_visitor_days"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10))
    # HMAC of the user id under the app secret; not reversible, not a key.
    visitor: Mapped[str] = mapped_column(String(32))
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    opened: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[bool] = mapped_column(Boolean, default=False)
    played: Mapped[bool] = mapped_column(Boolean, default=False)
    signed_up: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("day", "visitor", name="uq_analytics_visitor_day"),
        Index("ix_analytics_visitor", "visitor"),
    )


class AccessEvent(Base):
    """One sign-in, registration, failed attempt, or session first-seen.

    This table is the counterpart to the two above, and it is kept separate from
    them on purpose. It identifies people by design, recording address, email,
    and device. Keeping it in its own table and its own module means that the
    structure enforces the anonymity of the counters rather than a convention.

    `user_id` is a plain integer with no foreign key. An access log that
    disappeared when the account did would not serve its purpose, and guest
    cleanup deletes accounts on a schedule. `who` and `is_guest` are snapshots
    for the same reason, so a row still reads correctly after the account is
    gone.
    """

    __tablename__ = "access_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # session | login | register | login_failed
    kind: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The email for a registered account, or a label such as "Guest #12"
    # otherwise. For a failed sign-in, this holds the address that was tried,
    # which is the reason the row exists.
    who: Mapped[str] = mapped_column(String(320), default="")
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str] = mapped_column(String(45), default="")   # 45 = max IPv6
    country: Mapped[str] = mapped_column(String(16), default="")
    device: Mapped[str] = mapped_column(String(16), default="")
    user_agent: Mapped[str] = mapped_column(String(200), default="")


# Phase 14: the fallback under `tree.place_action`.
#
# Since SP2, reads filter on `branch_id` and `depth`. A node written without
# them is invisible to every page, every context build, and every memory pass.
# The failure is silent, because nothing raises an error. Every current writer
# places its nodes explicitly, but relying on that would also mean relying on
# every fixture, script, and test written from now on. The session therefore
# enforces the rule as rows travel to the database.
#
# This listener is registered here rather than in tree.py so that importing the
# models is enough to enable it. The invariant belongs to the rows, not to the
# module that usually writes them. The import sits inside the callback because
# tree.py imports this module.
@event.listens_for(Session, "before_flush")
def _place_new_nodes_on_the_tree(session, flush_context, instances):
    from . import tree

    tree.place_new_nodes(session)
