from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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

Name = Annotated[str, Field(max_length=NAME_MAX)]
Tags = Annotated[str, Field(max_length=TAGS_MAX)]
CardType = Annotated[str, Field(max_length=CARD_TYPE_MAX)]
Prose = Annotated[str, Field(max_length=PROSE_MAX)]
ScriptSource = Annotated[str, Field(max_length=SCRIPT_MAX)]
ActionText = Annotated[str, Field(max_length=ACTION_MAX)]


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


class ActionOut(ORMModel):
    id: int
    adventure_id: int
    index: int
    type: str
    text: str
    reasoning: str | None = None
    created_at: datetime


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
    actions: list[ActionOut] = []


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


class SettingsUpdate(BaseModel):
    endpoint_url: Annotated[str, Field(max_length=500)] | None = None  # VARCHAR(500)
    # Encryption expands the stored value ~4/3 into the same VARCHAR(500):
    # 256 plaintext chars is the largest safe input ("enc:" + Fernet + base64).
    api_key: Annotated[str, Field(max_length=256)] | None = None
    model: Name | None = None
    api_mode: Annotated[str, Field(max_length=20)] | None = None
    temperature: Annotated[float, Field(ge=0, le=5)] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=100_000)] | None = None
    reasoning_max_tokens: Annotated[int, Field(ge=0, le=100_000)] | None = None
    context_token_budget: Annotated[int, Field(ge=256, le=200_000)] | None = None
    narrator_prompt: Prose | None = None
    stream: bool | None = None
    summary_model: Name | None = None
    embedding_model: Name | None = None
    memory_bank_capacity: Annotated[int, Field(ge=1, le=1000)] | None = None
    memory_top_k: Annotated[int, Field(ge=1, le=50)] | None = None
