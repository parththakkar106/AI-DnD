from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Story cards ----------

class StoryCardBase(BaseModel):
    type: str = ""
    name: str = ""
    keys: str = ""
    entry: str = ""
    notes: str = ""


class StoryCardCreate(StoryCardBase):
    scenario_id: int | None = None
    adventure_id: int | None = None


class StoryCardUpdate(BaseModel):
    type: str | None = None
    name: str | None = None
    keys: str | None = None
    entry: str | None = None
    notes: str | None = None


class StoryCardOut(ORMModel, StoryCardBase):
    id: int
    scenario_id: int | None
    adventure_id: int | None


# ---------- Scenarios ----------

class ScenarioBase(BaseModel):
    title: str = "Untitled Scenario"
    description: str = ""
    prompt: str = ""
    memory: str = ""
    authors_note: str = ""
    ai_instructions: str = ""
    tags: str = ""


class ScenarioCreate(ScenarioBase):
    pass


class ScenarioUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    prompt: str | None = None
    memory: str | None = None
    authors_note: str | None = None
    ai_instructions: str | None = None
    tags: str | None = None
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
    title: str | None = None
    # ${Placeholder} values collected from the player at start (AI Dungeon behavior).
    placeholders: dict[str, str] = {}


class AdventureUpdate(BaseModel):
    title: str | None = None
    memory: str | None = None
    authors_note: str | None = None
    ai_instructions: str | None = None
    story_summary: str | None = None
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
    text: str


class ActionCreate(BaseModel):
    type: Literal["do", "say", "story", "continue"]
    text: str = ""


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
    text: str


class MemoryUpdate(BaseModel):
    text: str | None = None
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
    name: str = "Untitled Script"
    description: str = ""
    library_js: str = ""
    input_js: str = ""
    context_js: str = ""
    output_js: str = ""


class ScriptCreate(ScriptBase):
    pass


class ScriptUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    library_js: str | None = None
    input_js: str | None = None
    context_js: str | None = None
    output_js: str | None = None


class ScriptOut(ORMModel, ScriptBase):
    id: int
    created_at: datetime
    updated_at: datetime


class ScriptTestRequest(BaseModel):
    hook: Literal["input", "context", "output"]
    text: str = ""
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


class AdventureScriptUpdate(BaseModel):
    enabled: bool | None = None
    library_js: str | None = None
    input_js: str | None = None
    context_js: str | None = None
    output_js: str | None = None


# ---------- Auth (Phase 8) ----------

class AuthCredentials(BaseModel):
    email: str
    password: str


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
    endpoint_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    api_mode: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_max_tokens: int | None = None
    context_token_budget: int | None = None
    narrator_prompt: str | None = None
    stream: bool | None = None
    summary_model: str | None = None
    embedding_model: str | None = None
    memory_bank_capacity: int | None = None
    memory_top_k: int | None = None
