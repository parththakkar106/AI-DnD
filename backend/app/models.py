from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Table, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    # Phase 6: opt-in per adventure (extra AI calls)
    auto_summarize: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_bank_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # How many actions have already been folded into memories / the story summary.
    memory_cursor: Mapped[int] = mapped_column(Integer, default=0)
    summary_cursor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    scenario: Mapped[Scenario | None] = relationship(back_populates="adventures")
    story_cards: Mapped[list["StoryCard"]] = relationship(
        back_populates="adventure", cascade="all, delete-orphan"
    )
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


class Memory(Base):
    """Phase 6: an auto-summarized (or hand-written) fact about the adventure.

    `embedding` is the raw vector as a JSON list (cosine ranking happens in
    Python — fine at bank sizes of a few hundred). NULL until embedded, which
    also marks it for backfill when an embedding model becomes available.
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Action index range this memory summarizes (null for manual memories).
    source_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    forgotten: Mapped[bool] = mapped_column(Boolean, default=False)  # evicted, kept for UI
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    adventure: Mapped[Adventure] = relationship(back_populates="memories")

    @property
    def embedded(self) -> bool:
        return self.embedding is not None


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

    scenario: Mapped[Scenario | None] = relationship(back_populates="story_cards")
    adventure: Mapped[Adventure | None] = relationship(back_populates="story_cards")


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(20))  # start|do|say|story|continue|ai
    text: Mapped[str] = mapped_column(Text, default="")
    # Reasoning-model "thinking" that preceded the text (AI actions only).
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    adventure: Mapped[Adventure] = relationship(back_populates="actions")


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
    change running adventures. `state` lives on Adventure.script_state (one
    shared state per adventure, as in AI Dungeon)."""

    __tablename__ = "adventure_scripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    adventure_id: Mapped[int] = mapped_column(ForeignKey("adventures.id", ondelete="CASCADE"))
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
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=400)
    # Separate thinking budget for reasoning models (OpenRouter-style
    # `reasoning: {max_tokens}`); 0 = param not sent. Added on top of
    # max_output_tokens so story output keeps its full budget.
    reasoning_max_tokens: Mapped[int] = mapped_column(Integer, default=0)
    context_token_budget: Mapped[int] = mapped_column(Integer, default=4096)
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
    memory_bank_capacity: Mapped[int] = mapped_column(Integer, default=200)
    memory_top_k: Mapped[int] = mapped_column(Integer, default=5)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def api_key_plain(self) -> str:
        from . import security  # local import: models is imported before security

        return security.decrypt_secret(self.api_key)
