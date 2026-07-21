"""Context assembly per AI Dungeon's memory system
(help.aidungeon.com/faq/the-memory-system):

    [AI Instructions]        always included
    [Plot Essentials]        always included (classic "Memory")
    [Story Summary]          always included (manual in Phase 3, auto in Phase 6)
    [Used Memories]          top-K memory-bank retrievals (Phase 6, when enabled)
    [Triggered Story Cards]  "World Lore: <entry>", conditional; first dropped when over budget
    [Story history]          newest actions that fit the remaining token budget
    [Author's Note]          injected AUTHORS_NOTE_DEPTH actions before the end of history
    [Latest player action]   (+ script frontMemory right after it, Phase 4)
"""

import functools
from dataclasses import dataclass

import tiktoken

from .. import models, worldstate

AUTHORS_NOTE_DEPTH = 3  # actions from the end of history
CARD_BUDGET_SHARE = 0.4  # max share of non-reserved budget that story cards may take
SEPARATOR = "\n\n"


@functools.lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


def truncate_to_last_tokens(text: str, budget: int) -> str:
    tokens = _encoding().encode(text)
    if len(tokens) <= budget:
        return text
    return _encoding().decode(tokens[-budget:])


@dataclass
class Section:
    label: str
    text: str

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)


def _script_memory(adventure: models.Adventure) -> dict:
    """Script-provided memory overrides (populated by Phase 4 scripting)."""
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    memory = state.get("memory")
    return memory if isinstance(memory, dict) else {}


def _visible_npcs(adventure: models.Adventure, stat_schema: dict) -> dict[str, str]:
    """NPC story cards (by schema-configured type) whose keys appear in the
    recent story — the ones "in scene", so only their stats get injected."""
    actions = [a for a in adventure.actions if a.text.strip()]
    recent = SEPARATOR.join(a.text for a in actions[-6:]).lower()
    types = worldstate.npc_types(stat_schema)
    visible: dict[str, str] = {}
    for card in adventure.story_cards:
        if (card.type or "").lower() not in types:
            continue
        for key in (k.strip().lower() for k in card.keys.split(",")):
            if key and key in recent:
                visible[str(card.id)] = card.name or f"NPC {card.id}"
                break
    return visible


def _match_cards(cards: list[models.StoryCard], window_text: str) -> list[dict]:
    """AI Dungeon trigger rules: case-insensitive, space-sensitive, partial-word
    ('boat' triggers on 'boats'). Returns one record per card with the keyword that fired."""
    haystack = window_text.lower()
    matched = []
    for card in cards:
        for key in (k.strip().lower() for k in card.keys.split(",")):
            if key and key in haystack:
                matched.append(
                    {"id": card.id, "name": card.name, "keyword": key, "entry": card.entry}
                )
                break
    return matched


def build_context(
    adventure: models.Adventure,
    settings: models.Settings,
    memory_bank: dict | None = None,
) -> tuple[str, str, dict]:
    """Returns (system_text, story_text, context_report). `memory_bank` is the
    result of memorybank.retrieve_memories (None when the bank is off)."""
    script_mem = _script_memory(adventure)

    # ----- Always-included components -----
    system_sections: list[Section] = [Section("narrator", settings.narrator_prompt.strip())]

    # RPG world state (Phase 12): current stats/milestones + how to report changes.
    stat_schema = adventure.scenario.stat_schema if adventure.scenario else None
    if worldstate.has_schema(stat_schema):
        guide = worldstate.render_reference(stat_schema)
        if guide:
            system_sections.append(Section("world_state_guide", guide))
        block = worldstate.render_state_section(
            adventure.world_state, stat_schema, _visible_npcs(adventure, stat_schema)
        )
        if block:
            system_sections.append(Section("world_state", block))
        system_sections.append(Section("world_state_rule", worldstate.EMIT_RULE))

    if isinstance(script_mem.get("context"), str) and script_mem["context"].strip():
        system_sections.append(Section("script_context", script_mem["context"].strip()))
    if adventure.ai_instructions.strip():
        system_sections.append(Section("ai_instructions", adventure.ai_instructions.strip()))
    if adventure.memory.strip():
        system_sections.append(
            Section("plot_essentials", f"Plot essentials:\n{adventure.memory.strip()}")
        )
    if adventure.story_summary.strip():
        system_sections.append(
            Section("story_summary", f"Story summary:\n{adventure.story_summary.strip()}")
        )
    if memory_bank and memory_bank.get("used"):
        lines = "\n".join(f"- {m['text']}" for m in memory_bank["used"])
        system_sections.append(Section("used_memories", f"Memories:\n{lines}"))

    authors_note_text = adventure.authors_note.strip()
    if isinstance(script_mem.get("authorsNote"), str) and script_mem["authorsNote"].strip():
        authors_note_text = script_mem["authorsNote"].strip()
    authors_note = f"[Author's note: {authors_note_text}]" if authors_note_text else ""

    front_memory = ""
    if isinstance(script_mem.get("frontMemory"), str):
        front_memory = script_mem["frontMemory"].strip()

    reserved = (
        sum(s.tokens for s in system_sections)
        + count_tokens(authors_note)
        + count_tokens(front_memory)
    )
    available = max(256, settings.context_token_budget - reserved)

    # ----- Story cards: triggered by recent story text (the window history could fill) -----
    actions = [a for a in adventure.actions if a.text.strip()]
    trigger_window = truncate_to_last_tokens(SEPARATOR.join(a.text for a in actions), available)
    triggered = _match_cards(adventure.story_cards, trigger_window)

    card_budget = int(available * CARD_BUDGET_SHARE)
    card_records = []
    lore_lines: list[str] = []
    used = 0
    for match in triggered:
        line = f"World Lore: {match['entry'].strip()}"
        tokens = count_tokens(line)
        included = used + tokens <= card_budget
        if included:
            lore_lines.append(line)
            used += tokens
        card_records.append(
            {"id": match["id"], "name": match["name"], "keyword": match["keyword"],
             "included": included}
        )
    if lore_lines:
        system_sections.append(Section("world_lore", "\n".join(lore_lines)))

    # ----- Story history: newest first until the remaining budget is spent -----
    history_budget = available - used
    included_actions: list[models.Action] = []
    spent = 0
    oldest_truncated = False
    for action in reversed(actions):
        tokens = count_tokens(action.text) + count_tokens(SEPARATOR)
        if spent + tokens > history_budget:
            if not included_actions:
                # Even the newest action alone is over budget: hard-truncate it.
                included_actions.append(
                    models.Action(
                        adventure_id=action.adventure_id, index=action.index,
                        type=action.type,
                        text=truncate_to_last_tokens(action.text, history_budget),
                    )
                )
                oldest_truncated = True
            break
        included_actions.append(action)
        spent += tokens
    included_actions.reverse()

    # ----- Assemble story text with author's note near the end -----
    texts = [a.text for a in included_actions]
    note_sections: list[Section] = []
    if authors_note:
        pos = max(0, len(texts) - AUTHORS_NOTE_DEPTH)
        before, after = texts[:pos], texts[pos:]
        if before:
            note_sections.append(Section("history", SEPARATOR.join(before)))
        note_sections.append(Section("authors_note", authors_note))
        note_sections.append(Section("recent_history", SEPARATOR.join(after)))
    else:
        note_sections.append(Section("history", SEPARATOR.join(texts)))
    if front_memory:
        note_sections.append(Section("front_memory", front_memory))

    story_sections = [s for s in note_sections if s.text]
    system_text = SEPARATOR.join(s.text for s in system_sections if s.text)
    story_text = SEPARATOR.join(s.text for s in story_sections)

    all_sections = [s for s in system_sections if s.text] + story_sections
    report = {
        "sections": [
            {"label": s.label, "text": s.text, "tokens": s.tokens} for s in all_sections
        ],
        "prompt": {"system": system_text, "story": story_text},
        "tokens": {
            "total": count_tokens(system_text) + count_tokens(story_text),
            "budget": settings.context_token_budget,
        },
        "cards": card_records,
        "memories": memory_bank,
        "history": {
            "included": len(included_actions),
            "total": len(actions),
            "oldest_truncated": oldest_truncated,
        },
        "settings": {
            "model": settings.model,
            "api_mode": settings.api_mode,
            "temperature": settings.temperature,
            "max_output_tokens": settings.max_output_tokens,
        },
    }
    return system_text, story_text, report
