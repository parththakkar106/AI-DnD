from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from . import images

# Length caps (Phase 9). The VARCHAR ones are correctness, not just abuse
# limits: Postgres enforces column lengths (SQLite never did), so anything
# longer must be a 422 here rather than a 500 at INSERT. Text-column caps are
# generous abuse ceilings a legitimate player won't hit.
NAME_MAX = 200          # titles/names — VARCHAR(200)
TAGS_MAX = 500          # VARCHAR(500)
CARD_TYPE_MAX = 100     # VARCHAR(100)
PROSE_MAX = 50_000      # memory, author's note, prompts, entries, notes...
SCRIPT_MAX = 200_000    # one JS source
ACTION_MAX = 20_000     # one player action
MEMORY_TEXT_MAX = 5_000
# A scenario cover image, stored inline as a base64 data URI. 400x300 WebP at
# the quality the editor encodes lands around 20-40 KB; 400 KB leaves room for
# a client that downscales less aggressively without letting anyone park a
# multi-megabyte PNG in a row that gets read on every list request.
IMAGE_MAX = 400_000
ICON_MAX = 16           # one emoji/glyph — VARCHAR(16)
BRANCH_NAME_MAX = 80    # what a player called one line of the story — VARCHAR(80)

Name = Annotated[str, Field(max_length=NAME_MAX)]
Tags = Annotated[str, Field(max_length=TAGS_MAX)]
CardType = Annotated[str, Field(max_length=CARD_TYPE_MAX)]
Prose = Annotated[str, Field(max_length=PROSE_MAX)]
ScriptSource = Annotated[str, Field(max_length=SCRIPT_MAX)]
ActionText = Annotated[str, Field(max_length=ACTION_MAX)]
Image = Annotated[str, Field(max_length=IMAGE_MAX)]
Icon = Annotated[str, Field(max_length=ICON_MAX)]


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
    # Cover art — an https URL or a base64 data URI. See app/images.py.
    image: Image = ""
    # Emoji/glyph shown when `image` is empty.
    icon: Icon = ""
    # Phase 12: RPG world-state template (stat defs, bands, rules, milestones).
    # None means no RPG layer.
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
    is_public: bool = False  # shared demo content — read-only for everyone
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
    # Read off the row so `image_url` can be derived, but excluded from the
    # response: a list of base64 data URIs would be megabytes of JSON.
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
    # ${Placeholder} values collected from the player at start (AI Dungeon behavior).
    placeholders: dict[str, str] = {}


class AdventureUpdate(BaseModel):
    title: Name | None = None
    memory: Prose | None = None
    authors_note: Prose | None = None
    ai_instructions: Prose | None = None
    story_summary: Prose | None = None
    auto_summarize: bool | None = None
    memory_bank_enabled: bool | None = None


class AdventureRefresh(BaseModel):
    """Body for "Update from scenario". `placeholders` supplies answers the
    adventure has no stored value for (see AdventureCreate.placeholders); they
    are merged over the stored ones and saved."""

    placeholders: dict[str, str] = {}


class RefreshPlan(BaseModel):
    """What a refresh would change — drives the confirm dialog."""

    scenario_id: int
    scenario_title: str
    has_changes: bool
    # field name -> {"old": ..., "new": ...}, only for fields that differ.
    fields: dict[str, dict] = {}
    # {"added"|"updated"|"removed": [card name, ...]}
    cards: dict[str, list[str]] = {}
    # {"added"|"removed": [stat path, ...]} — live values are otherwise kept.
    world_state: dict[str, list[str]] = {}
    # ${Placeholder} names the scenario asks for that the adventure has no
    # stored answer to; the client must collect these and send them back.
    placeholders_needed: list[str] = []


class ActionOut(ORMModel):
    id: int
    adventure_id: int
    index: int
    type: str
    text: str
    reasoning: str | None = None
    # Phase 12: compact RPG state changes for this turn (from the model property).
    world_changes: list[dict] = []
    # Retry history: how many attempts exist for this turn (0 = never retried)
    # and which one is live. The attempts themselves come from
    # GET /actions/{id}/variants so this payload stays small.
    variant_count: int = 0
    variant_index: int = 0
    created_at: datetime


class VariantOut(BaseModel):
    index: int
    text: str
    reasoning: str | None = None
    created_at: str | None = None
    active: bool = False


class VariantSelect(BaseModel):
    index: int = Field(ge=0)


class BranchOut(ORMModel):
    """One line through the story tree (Phase 14, SP5).

    Enough to draw the tree and nothing more: `fork_depth` is where this line
    leaves its parent and `depth` is where it currently ends, so a fork is two
    numbers rather than a walk. `own_actions` counts the turns played on this
    branch itself — the rest of its story is borrowed from its ancestors, which
    is the whole point and also why the number is smaller than the reader
    expects.
    """

    id: int
    parent_branch_id: int | None = None
    fork_depth: int | None = None
    depth: int
    own_actions: int = 0
    is_head: bool = False
    # NULL for a branch nobody has named. The client draws those from the fork
    # depth rather than the server inventing one — see the column comment.
    name: str | None = None
    created_at: datetime


class BranchRename(BaseModel):
    """A name a player chose, or `null` to go back to being unnamed."""

    name: Annotated[str, Field(max_length=BRANCH_NAME_MAX)] | None = None


class ActionUpdate(BaseModel):
    text: ActionText


class ActionCreate(BaseModel):
    type: Literal["do", "say", "story", "continue"]
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
    created_at: datetime
    updated_at: datetime
    story_cards: list[StoryCardOut] = []
    # The NEWEST window of the story, not all of it — older pages arrive from
    # GET /{id}/actions as the reader scrolls up. `action_count` is the whole
    # story's length, which is how the client knows there is more above.
    actions: list[ActionOut] = []
    action_count: int = 0


class ActionPage(BaseModel):
    """A slice of the story, counted back from the newest action."""

    actions: list[ActionOut] = []
    total: int = 0
    # Whether anything older than this slice exists. Computed server-side so
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
    # "Where you left off" — the tail of the most recent narrative beat, so a
    # Continue card can show the story instead of just a turn count.
    snippet: str = ""
    # Cover art inherited from the parent scenario (see app/images.py).
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
    # Set by the router (not stored): True when a syncable library version
    # exists whose code differs from this copy; None when nothing to sync.
    out_of_date: bool | None = None


class AdventureScriptUpdate(BaseModel):
    enabled: bool | None = None
    library_js: ScriptSource | None = None
    input_js: ScriptSource | None = None
    context_js: ScriptSource | None = None
    output_js: ScriptSource | None = None


# ---------- Auth (Phase 8) ----------

class AuthCredentials(BaseModel):
    email: Annotated[str, Field(max_length=320)]  # VARCHAR(320)
    # Upper bound keeps scrypt cost flat — hashing megabyte "passwords" is CPU
    # an attacker would otherwise get for free.
    password: Annotated[str, Field(max_length=128)]


# ---------- Settings ----------

class SettingsOut(ORMModel):
    endpoint_url: str
    # The key itself is never echoed back (encrypted at rest, write-only).
    has_api_key: bool
    model: str
    api_mode: str
    temperature: float
    max_output_tokens: int
    reasoning_max_tokens: int
    context_token_budget: int
    narrator_prompt: str
    stream: bool
    summary_model: str
    embedding_model: str
    memory_bank_capacity: int
    memory_top_k: int


ScenarioOut.model_rebuild()


# ---------- AI Chat (power users) ----------
# A scratchpad for talking to a model directly, with no story framing. Nothing
# is persisted server-side, so these caps are purely per-request abuse limits.

CHAT_MESSAGE_MAX = 100_000   # one message
CHAT_TOTAL_MAX = 400_000     # whole conversation sent up per request
CHAT_MESSAGES_MAX = 200      # turns per request


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(max_length=CHAT_MESSAGE_MAX)]


class ChatRequest(BaseModel):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=CHAT_MESSAGES_MAX)]
    # Empty/omitted = fall back to the user's configured model.
    model: Name | None = None
    temperature: Annotated[float, Field(ge=0, le=5)] | None = None
    max_tokens: Annotated[int, Field(ge=1, le=100_000)] | None = None


class SettingsUpdate(BaseModel):
    endpoint_url: Annotated[str, Field(max_length=500)] | None = None  # VARCHAR(500)
    # Encryption expands the stored value ~4/3 into the same VARCHAR(500):
    # 256 plaintext chars is the largest safe input ("enc:" + Fernet + base64).
    api_key: Annotated[str, Field(max_length=256)] | None = None
    model: Name | None = None
    api_mode: Annotated[str, Field(max_length=20)] | None = None
    temperature: Annotated[float, Field(ge=0, le=5)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=100_000)] | None = None
    # -1 = explicitly off (sends `reasoning: {effort: none}`); 0 = send nothing.
    reasoning_max_tokens: Annotated[int, Field(ge=-1, le=100_000)] | None = None
    context_token_budget: Annotated[int, Field(ge=256, le=200_000)] | None = None
    narrator_prompt: Prose | None = None
    stream: bool | None = None
    summary_model: Name | None = None
    embedding_model: Name | None = None
    memory_bank_capacity: Annotated[int, Field(ge=1, le=1000)] | None = None
    memory_top_k: Annotated[int, Field(ge=1, le=50)] | None = None
