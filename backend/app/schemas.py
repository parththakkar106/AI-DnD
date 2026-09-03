from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from . import images

# Length caps (Phase 9). The VARCHAR caps are a correctness requirement rather
# than only an abuse limit. Postgres enforces column lengths and SQLite never
# did, so a longer value has to be a 422 here rather than a 500 at INSERT. The
# text-column caps are generous abuse ceilings that a legitimate player does not
# reach.
NAME_MAX = 200          # Titles and names. VARCHAR(200).
TAGS_MAX = 500          # VARCHAR(500).
CARD_TYPE_MAX = 100     # VARCHAR(100).
PROSE_MAX = 50_000      # Memory, author's note, prompts, entries, and notes.
SCRIPT_MAX = 200_000    # One JavaScript source file.
ACTION_MAX = 20_000     # One player action.
MEMORY_TEXT_MAX = 5_000
# A scenario cover image, stored inline as a base64 data URI. A 400x300 WebP at
# the quality the editor encodes runs about 20 to 40 kB. A cap of 400 kB leaves
# room for a client that downscales less aggressively, and it stops anyone from
# storing a multi-megabyte PNG in a row that every list request reads.
IMAGE_MAX = 400_000
ICON_MAX = 16           # One emoji or glyph. VARCHAR(16).
BRANCH_NAME_MAX = 80    # What a player called one line of the story. VARCHAR(80).
PERSONA_NAME_MAX = 80   # The protagonist's name. VARCHAR(80).
PERSONA_PRONOUNS_MAX = 40   # "they/them" and the like. VARCHAR(40).

Name = Annotated[str, Field(max_length=NAME_MAX)]
Tags = Annotated[str, Field(max_length=TAGS_MAX)]
CardType = Annotated[str, Field(max_length=CARD_TYPE_MAX)]
Prose = Annotated[str, Field(max_length=PROSE_MAX)]
ScriptSource = Annotated[str, Field(max_length=SCRIPT_MAX)]
ActionText = Annotated[str, Field(max_length=ACTION_MAX)]
Image = Annotated[str, Field(max_length=IMAGE_MAX)]
Icon = Annotated[str, Field(max_length=ICON_MAX)]
PersonaName = Annotated[str, Field(max_length=PERSONA_NAME_MAX)]
PersonaPronouns = Annotated[str, Field(max_length=PERSONA_PRONOUNS_MAX)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Story cards ----------

class StoryCardBase(BaseModel):
    type: CardType = ""
    name: Name = ""
    keys: Prose = ""
    entry: Prose = ""
    notes: Prose = ""


class StoryCardCreate(StoryCardBase):
    scenario_id: int | None = None
    adventure_id: int | None = None


class StoryCardUpdate(BaseModel):
    type: CardType | None = None
    name: Name | None = None
    keys: Prose | None = None
    entry: Prose | None = None
    notes: Prose | None = None


class StoryCardOut(ORMModel, StoryCardBase):
    id: int
    scenario_id: int | None
    adventure_id: int | None


# ---------- Scenarios ----------

class ScenarioBase(BaseModel):
    title: Name = "Untitled Scenario"
    description: Prose = ""
    prompt: Prose = ""
    memory: Prose = ""
    authors_note: Prose = ""
    ai_instructions: Prose = ""
    tags: Tags = ""
    # Cover art, either an https URL or a base64 data URI. See `app/images.py`.
    image: Image = ""
    # The emoji or glyph shown when `image` is empty.
    icon: Icon = ""
    # Phase 12: the RPG world-state template, holding stat definitions, bands,
    # rules, and milestones. `None` means the scenario has no RPG layer.
    stat_schema: dict | None = None


class ScenarioCreate(ScenarioBase):
    pass


class ScenarioUpdate(BaseModel):
    title: Name | None = None
    description: Prose | None = None
    prompt: Prose | None = None
    memory: Prose | None = None
    authors_note: Prose | None = None
    ai_instructions: Prose | None = None
    tags: Tags | None = None
    image: Image | None = None
    icon: Icon | None = None
    stat_schema: dict | None = None
    script_ids: list[int] | None = None


class ScenarioOut(ORMModel, ScenarioBase):
    id: int
    is_public: bool = False  # Shared demo content, read-only for everyone.
    created_at: datetime
    updated_at: datetime
    story_cards: list[StoryCardOut] = []
    scripts: list["ScriptOut"] = []


class ScenarioListItem(ORMModel):
    id: int
    title: str
    description: str
    tags: str
    is_public: bool = False
    updated_at: datetime
    # Read from the row so that `image_url` can be derived, and excluded from
    # the response, because a list of base64 data URIs would be megabytes of
    # JSON.
    image: str = Field("", exclude=True)
    icon: str = ""

    @computed_field
    @property
    def image_url(self) -> str:
        return images.public_url(self.id, self.image, self.updated_at)


# ---------- Adventures ----------

class AdventureCreate(BaseModel):
    scenario_id: int | None = None
    title: Name | None = None
    # The `${Placeholder}` values collected from the player at the start, which
    # is the AI Dungeon behavior.
    placeholders: dict[str, str] = {}
    # Phase 18: who the player is playing as, collected by the same modal. These
    # are independent of `placeholders`: a scenario that asks for `${Name}` is
    # asking its own question, and nothing here fills it in.
    persona_name: PersonaName = ""
    persona_pronouns: PersonaPronouns = ""
    persona_desc: Prose = ""


class AdventureUpdate(BaseModel):
    title: Name | None = None
    memory: Prose | None = None
    authors_note: Prose | None = None
    ai_instructions: Prose | None = None
    story_summary: Prose | None = None
    auto_summarize: bool | None = None
    memory_bank_enabled: bool | None = None
    persona_name: PersonaName | None = None
    persona_pronouns: PersonaPronouns | None = None
    persona_desc: Prose | None = None


class AdventureRefresh(BaseModel):
    """The body for "Update from scenario".

    `placeholders` supplies answers the adventure has no stored value for. See
    `AdventureCreate.placeholders`. The answers are merged over the stored ones
    and saved.
    """

    placeholders: dict[str, str] = {}


class RefreshPlan(BaseModel):
    """What a refresh would change. The confirm dialog is built from this."""

    scenario_id: int
    scenario_title: str
    has_changes: bool
    # Maps a field name to `{"old": ..., "new": ...}`, for differing fields
    # only.
    fields: dict[str, dict] = {}
    # Maps "added", "updated", or "removed" to a list of card names.
    cards: dict[str, list[str]] = {}
    # Maps "added" or "removed" to a list of stat paths. Live values are
    # otherwise kept.
    world_state: dict[str, list[str]] = {}
    # The `${Placeholder}` names the scenario asks for that the adventure has
    # no stored answer to. The client collects these and sends them back.
    placeholders_needed: list[str] = []


class ActionOut(ORMModel):
    id: int
    adventure_id: int
    type: str
    text: str
    reasoning: str | None = None
    # Phase 12: the compact RPG state changes for this turn, read from the
    # model property.
    world_changes: list[dict] = []
    # True when those changes are a player's correction of the turn rather than
    # the model's own proposal. See `PUT /actions/{id}/world-delta`.
    world_changes_revised: bool = False
    # SP9: the pager, such as `2/4`. It reports how many attempts this turn has
    # and which one is on screen. It is keyed on the parent, so it counts the
    # attempts of this turn rather than every node that shares a depth, and it
    # keeps counting them after one has been forked onto its own branch.
    #
    # A turn nobody has retaken reads 1/1, which is most turns, and the client
    # draws no pager for a count of one. The attempts themselves come from
    # `GET /actions/{id}/variants`, so this payload stays small.
    take_count: int = 1
    take_index: int = 0
    # Which line this node is on, so the pager can distinguish the two kinds of
    # step without asking the server. An attempt on this branch is a leaf with
    # nothing below it, so showing it is a local change. An attempt on another
    # branch has a story of its own, so moving to it is a branch switch.
    branch_id: int | None = None
    created_at: datetime


class VariantOut(BaseModel):
    # Since SP4 every attempt is its own node, so each one has an id, and the
    # client needs that id. A fork is addressed by the attempt being promoted,
    # not by its position in a group that renumbers whenever an attempt is
    # added.
    id: int
    index: int
    text: str
    reasoning: str | None = None
    # See `ActionOut.branch_id`. It decides whether choosing this attempt is a
    # local step or a branch switch.
    branch_id: int | None = None
    created_at: str | None = None
    active: bool = False


class VariantSelect(BaseModel):
    index: int = Field(ge=0)


class BranchOut(ORMModel):
    """One line through the story tree (Phase 14, SP5).

    This carries enough to draw the tree and nothing more. `fork_depth` is where
    this line leaves its parent, and `depth` is where it currently ends, so a
    fork is two numbers rather than a walk. `own_actions` counts the turns played
    on this branch itself. The rest of its story is borrowed from its ancestors,
    which is why the number is smaller than a reader expects.
    """

    id: int
    parent_branch_id: int | None = None
    fork_depth: int | None = None
    depth: int
    own_actions: int = 0
    is_head: bool = False
    # NULL for a branch nobody has named. The client labels those from the fork
    # depth rather than the server inventing a name. See the column comment.
    name: str | None = None
    created_at: datetime


class BranchRename(BaseModel):
    """A name a player chose, or `null` to make the branch unnamed again."""

    name: Annotated[str, Field(max_length=BRANCH_NAME_MAX)] | None = None


class ActionUpdate(BaseModel):
    text: ActionText


class ActionCreate(BaseModel):
    type: Literal["do", "say", "story", "continue"]
    text: ActionText = ""
    # The node this action is played after (SP9). Omitting it means the tip,
    # which is what every ordinary turn uses.
    #
    # Naming an attempt the story moved past is what creates a branch. Stepping
    # between attempts costs nothing and creates nothing, and the fork happens
    # on the first text written below one. That is the first moment the player
    # states which line they mean. Before it, they were reading.
    after_id: int | None = None


class TakeCreate(BaseModel):
    """Another attempt at a turn (SP9).

    `text` is what the player says instead, and it applies only when the turn was
    the player's. An AI turn's other attempt is generated, so the field is
    ignored there rather than rejected. The client makes the same request for
    both, and the node type decides what happens.
    """

    text: ActionText = ""


class AdventureOut(ORMModel):
    id: int
    scenario_id: int | None
    title: str
    memory: str
    authors_note: str
    ai_instructions: str
    story_summary: str
    auto_summarize: bool
    memory_bank_enabled: bool
    persona_name: str
    persona_pronouns: str
    persona_desc: str
    created_at: datetime
    updated_at: datetime
    story_cards: list[StoryCardOut] = []
    # The newest window of the story, not all of it. Older pages arrive from
    # `GET /{id}/actions` as the reader scrolls up. `action_count` is the whole
    # story's length, which is how the client knows more actions exist above.
    actions: list[ActionOut] = []
    action_count: int = 0


class ActionPage(BaseModel):
    """A slice of the story, counted back from the newest action."""

    actions: list[ActionOut] = []
    total: int = 0
    # Whether anything older than this slice exists. The server computes it, so
    # the client never has to do arithmetic on positions to find the end.
    has_more: bool = False


# ---------- Memory bank (Phase 6) ----------

class MemoryOut(ORMModel):
    id: int
    adventure_id: int
    text: str
    pinned: bool
    forgotten: bool
    embedded: bool  # model property: embedding vector present
    use_count: int
    last_used_at: datetime | None
    source_start: int | None
    source_end: int | None
    created_at: datetime


class MemoryCreate(BaseModel):
    text: Annotated[str, Field(max_length=MEMORY_TEXT_MAX)]


class MemoryUpdate(BaseModel):
    text: Annotated[str, Field(max_length=MEMORY_TEXT_MAX)] | None = None
    pinned: bool | None = None
    forgotten: bool | None = None


class AdventureListItem(ORMModel):
    id: int
    scenario_id: int | None
    scenario_title: str | None = None
    title: str
    updated_at: datetime
    action_count: int = 0
    # The end of the most recent narration, so a Continue card can show the
    # story rather than only a turn count.
    snippet: str = ""
    # Cover art inherited from the parent scenario. See `app/images.py`.
    image_url: str = ""
    icon: str = ""


# ---------- Scripts ----------

class ScriptBase(BaseModel):
    name: Name = "Untitled Script"
    description: Prose = ""
    library_js: ScriptSource = ""
    input_js: ScriptSource = ""
    context_js: ScriptSource = ""
    output_js: ScriptSource = ""


class ScriptCreate(ScriptBase):
    pass


class ScriptUpdate(BaseModel):
    name: Name | None = None
    description: Prose | None = None
    library_js: ScriptSource | None = None
    input_js: ScriptSource | None = None
    context_js: ScriptSource | None = None
    output_js: ScriptSource | None = None


class ScriptOut(ORMModel, ScriptBase):
    id: int
    created_at: datetime
    updated_at: datetime


class ScriptTestRequest(BaseModel):
    hook: Literal["input", "context", "output"]
    text: Prose = ""
    state: dict = {}


class AdventureScriptOut(ORMModel):
    id: int
    adventure_id: int
    position: int
    enabled: bool
    name: str
    description: str
    library_js: str
    input_js: str
    context_js: str
    output_js: str
    # The router sets this field, which is not stored. It is `True` when a
    # syncable library version exists whose code differs from this copy, and
    # `None` when there is nothing to sync from.
    out_of_date: bool | None = None


class AdventureScriptUpdate(BaseModel):
    enabled: bool | None = None
    library_js: ScriptSource | None = None
    input_js: ScriptSource | None = None
    context_js: ScriptSource | None = None
    output_js: ScriptSource | None = None


# ---------- Auth (Phase 8) ----------

class AuthCredentials(BaseModel):
    email: Annotated[str, Field(max_length=320)]  # VARCHAR(320).
    # The upper bound keeps the scrypt cost constant. Without it, hashing a
    # megabyte password would give an attacker free CPU time.
    password: Annotated[str, Field(max_length=128)]


# ---------- Settings ----------

class SettingsOut(ORMModel):
    endpoint_url: str
    # The key itself is never returned. It is encrypted at rest and
    # write-only.
    has_api_key: bool
    model: str
    api_mode: str
    temperature: float
    max_output_tokens: int
    reasoning_max_tokens: int
    context_token_budget: int
    narrator_prompt: str
    summary_model: str
    embedding_model: str
    memory_bank_capacity: int
    memory_top_k: int


ScenarioOut.model_rebuild()


# ---------- AI Chat (power users) ----------
# A scratchpad for talking to a model directly, with no story framing. The
# server persists nothing, so these caps are per-request abuse limits only.

CHAT_MESSAGE_MAX = 100_000   # One message.
CHAT_TOTAL_MAX = 400_000     # The whole conversation sent per request.
CHAT_MESSAGES_MAX = 200      # Turns per request.


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(max_length=CHAT_MESSAGE_MAX)]


class ChatRequest(BaseModel):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=CHAT_MESSAGES_MAX)]
    # If this field is empty or omitted, the user's configured model is used.
    model: Name | None = None
    temperature: Annotated[float, Field(ge=0, le=5)] | None = None
    max_tokens: Annotated[int, Field(ge=1, le=100_000)] | None = None


class SettingsUpdate(BaseModel):
    endpoint_url: Annotated[str, Field(max_length=500)] | None = None  # VARCHAR(500).
    # Encryption expands the stored value by about four thirds into the same
    # VARCHAR(500), so 256 plaintext characters is the largest safe input. The
    # stored form is "enc:" plus Fernet plus base64.
    api_key: Annotated[str, Field(max_length=256)] | None = None
    model: Name | None = None
    api_mode: Annotated[str, Field(max_length=20)] | None = None
    temperature: Annotated[float, Field(ge=0, le=5)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=100_000)] | None = None
    # A value of -1 turns reasoning off explicitly, which sends
    # `reasoning: {effort: none}`. A value of 0 sends nothing.
    reasoning_max_tokens: Annotated[int, Field(ge=-1, le=100_000)] | None = None
    context_token_budget: Annotated[int, Field(ge=256, le=200_000)] | None = None
    narrator_prompt: Prose | None = None
    summary_model: Name | None = None
    embedding_model: Name | None = None
    memory_bank_capacity: Annotated[int, Field(ge=1, le=1000)] | None = None
    memory_top_k: Annotated[int, Field(ge=1, le=50)] | None = None
