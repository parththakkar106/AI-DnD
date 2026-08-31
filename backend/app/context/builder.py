"""Context assembly per AI Dungeon's memory system
(help.aidungeon.com/faq/the-memory-system):

    [AI Instructions]        always included
    [Player Character]       always included when the adventure has a persona
    [Plot Essentials]        always included (classic "Memory")
    [Story Summary]          always included (manual in Phase 3, auto in Phase 6)
    [Used Memories]          top-K memory-bank retrievals (Phase 6, when enabled)
    [Triggered Story Cards]  "World Lore: <entry>", conditional; first dropped when over budget
    [Story history]          newest actions that fit the remaining token budget
    [Author's Note]          injected AUTHORS_NOTE_DEPTH actions before the end of history
    [Latest player action]   (+ script frontMemory right after it, Phase 4)

The list above comes from AI Dungeon's design. The order does not. This module
emits every fixed section first and every changing section after the history,
because prompt caching bills on a shared prefix. A section that changes near the
top of the prompt re-prices everything below it. See the comments on the static
block and the live sections in `build_context`.
"""

import functools
from dataclasses import dataclass

import tiktoken

from .. import models, worldstate
from . import history

AUTHORS_NOTE_DEPTH = 3  # actions from the end of history
CARD_BUDGET_SHARE = 0.4  # max share of non-reserved budget that story cards may take
NPC_WINDOW = 6  # actions of story searched for NPC trigger words ("in scene")
SEPARATOR = "\n\n"

# Output-length guidance. The endpoint enforces `max_output_tokens` as a hard
# limit, and it truncates the reply mid-sentence when the model reaches it. The
# state block is emitted last, so truncation removes it. Asking the model to
# finish inside the limit prevents the truncation.
LENGTH_HEADROOM = 50  # Tokens reserved from the cap for the state block.
# Models cannot count their own tokens, but they do follow a word budget, so the
# hint states a number of words. English prose averages 0.75 words per token.
WORDS_PER_TOKEN = 0.75
# Models regularly exceed a word budget, and the cap it protects is a hard
# limit. Aiming 10% below the real ceiling leaves room for that overshoot, so it
# does not consume the state block.
LENGTH_BUFFER = 0.90
MIN_LENGTH_HINT_WORDS = 40  # Below this, the hint adds nothing useful.
# A ceiling on its own gives one-sided guidance, and models respond to it
# differently. A verbose model treats it as a limit. A terse model has only the
# instruction to write as much as the moment needs, and it produces two
# paragraphs. Adding a floor turns the guidance into a range, so the same prompt
# produces a similar length from either model. The floor is a share of the
# ceiling so that it can never approach the ceiling.
LENGTH_FLOOR_SHARE = 0.35
# Below this word count, a floor means nothing, because a short turn is the
# correct turn at a tight cap. The wording used at a tight cap is also the
# wording that was measured to preserve the state block, so it is unchanged.
MIN_LENGTH_FLOOR_WORDS = 60
# The floor prevents a collapse to two paragraphs. It does not ask for an essay.
# At a 2400-token cap, the share alone would request a minimum of 555 words. A
# reader who wants longer turns can ask for them in the author's note.
MAX_LENGTH_FLOOR_WORDS = 300


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


def length_hint(max_output_tokens: int, *, has_ws: bool) -> str:
    """Ask for a turn that fits inside the output cap, stated as a word budget.

    Returns an empty string when the cap is too small to state usefully. The
    model can exceed the hint, so the hint earns its tokens only when there is
    enough room for that overshoot to stay inside the cap.
    """
    words = int((max_output_tokens - LENGTH_HEADROOM) * WORDS_PER_TOKEN * LENGTH_BUFFER)
    if words < MIN_LENGTH_HINT_WORDS:
        return ""
    tail = (
        " Finish the narration and append the state block well inside the limit."
        if has_ws
        else " Bring the turn to a close well inside the limit rather than "
        "stopping mid-sentence."
    )
    # State the number as a ceiling, never as a budget. In measurements, the
    # wording "keep this turn under about N words" read to the model as a target
    # to fill. It raised the average from 174 words to 246 across five runs, and
    # every hinted run was longer than every unhinted run. The hint therefore
    # pushed turns toward the limit it exists to avoid. Naming the number as a
    # limit, and adding that a typical turn is much shorter, held the average at
    # 170 while still preserving the state block at tight caps.
    floor = min(int(words * LENGTH_FLOOR_SHARE), MAX_LENGTH_FLOOR_WORDS)
    if floor < MIN_LENGTH_FLOOR_WORDS:
        return (
            f"[Hard limit: this turn must not exceed {words} words. Write only as "
            f"much as the moment needs — a typical turn is much shorter.{tail}]"
        )
    # Both numbers are bounds, and the wording is deliberately asymmetric. The
    # ceiling uses "must not exceed", because the endpoint enforces it. The floor
    # uses "should not stop short of". Neither reads as a target, which the
    # measurement above shows is what matters. The clause that asks the model to
    # prefer the lower end does the job the earlier wording did, which was to
    # keep a verbose model away from the ceiling. It now has a number beneath it,
    # so a terse model reading the same clause stops at the floor rather than at
    # forty words.
    return (
        f"[Hard limit: this turn must not exceed {words} words, and it should not "
        f"stop short of about {floor}. Prefer the lower end of that range unless "
        f"the scene genuinely needs more.{tail}]"
    )


def render_persona(adventure: models.Adventure) -> str:
    """Returns the Player Character section, or "" when there is no persona.

    The three fields are independent. A name alone is enough, a description
    alone is enough, and the wording holds together for either. Pronouns are
    stated because the summarizer in `memorybank` writes about the protagonist
    in the third person, and a model that has to infer a pronoun from a name
    will sometimes infer wrongly and then repeat that error in every memory it
    writes.
    """
    name = adventure.persona_name.strip()
    pronouns = adventure.persona_pronouns.strip()
    desc = adventure.persona_desc.strip()
    if not (name or desc):
        return ""
    head = f"You are {name}" if name else ""
    if head and pronouns:
        head += f" ({pronouns})"
    # Joined with a space, not `SEPARATOR`: this is one short paragraph about
    # one character, and a blank line inside it reads as two unrelated notes.
    body = " ".join(part for part in (f"{head}." if head else "", desc) if part)
    return f"Player character:\n{body}"


def _script_memory(adventure: models.Adventure) -> dict:
    """Script-provided memory overrides (populated by Phase 4 scripting)."""
    state = adventure.script_state if isinstance(adventure.script_state, dict) else {}
    memory = state.get("memory")
    return memory if isinstance(memory, dict) else {}


def _history_text(action: models.Action) -> str:
    """Returns an AI turn as the model should see it in replayed history.

    The result is the narration with its state block appended again,
    reconstructed from the stored delta. The app strips that block before
    storing and displaying the turn. Without this function, every past AI turn
    would appear to have emitted no state, and the model would copy that pattern
    and stop emitting state itself. Player turns and turns with no block pass
    through unchanged.

    The block replays the changes the engine ACCEPTED, not the ones the model
    sent. Replaying what was sent showed the model a refused change standing as
    though it had been applied, while the live values in the same prompt
    disagreed with it. Nothing marked which of the two was true, so the model
    read its own refused change as correct and sent it again.

    This function reads `world_delta` rather than `context_snapshot`. It runs
    for every action in the replayed history, and `context_snapshot` is deferred
    so that a turn never loads the prompt archive from the database.
    """
    text = action.text
    wd = action.world_delta if isinstance(action.world_delta, dict) else None
    if wd:
        block = worldstate.render_delta_block(worldstate.applied_delta(wd))
        if block:
            text = f"{text}\n{block}"
    return text


def _visible_npcs(actions: list[models.Action], stat_schema: dict) -> dict[str, str]:
    """Returns the NPCs whose trigger words appear in the recent story.

    These are the NPCs in scene, and the prompt includes stats for them only.
    The result maps an NPC id to its display name.

    `actions` holds only the most recent actions. See `NPC_WINDOW`.
    """
    recent = SEPARATOR.join(a.text for a in actions).lower()
    visible: dict[str, str] = {}
    for npc_key, ndef in (stat_schema.get("npcs") or {}).items():
        if not isinstance(ndef, dict):
            continue
        if any(trigger in recent for trigger in worldstate.npc_triggers(ndef, npc_key)):
            visible[npc_key] = worldstate.npc_name(ndef, npc_key)
    return visible


def _match_cards(cards: list[models.StoryCard], window_text: str) -> list[dict]:
    """Returns one record per matched story card, naming the keyword that matched.

    Matching follows AI Dungeon's rules. It ignores case, respects spaces, and
    matches partial words, so "boat" matches "boats".
    """
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
    exclude_action_id: int | None = None,
) -> tuple[str, str, dict]:
    """Returns (system_text, story_text, context_report). `memory_bank` is the
    result of memorybank.retrieve_memories (None when the bank is off);
    `exclude_action_id` omits one action from the story (see history.py)."""
    script_mem = _script_memory(adventure)

    # ----- The static block, which is identical on every turn -----
    # This ordering exists to reduce cost. Prompt caching matches a prefix. The
    # endpoint reuses the prompt up to the first byte that differs from the
    # previous request, and no further. A section that changes near the top
    # therefore re-prices everything below it, and what sits below it is the
    # story history, which is most of the prompt. Sections that change from turn
    # to turn go after the history, among the live sections. Placing them there
    # also gives them the most recency, which is why `EMIT_REMINDER` goes last.
    system_sections: list[Section] = [Section("narrator", settings.narrator_prompt.strip())]

    # RPG world state (Phase 12): the instructions for reporting changes. The
    # live values go into a live section below. The guide derived from the
    # schema and the emit rule do not change while the scenario is unchanged.
    stat_schema = adventure.scenario.stat_schema if adventure.scenario else None
    has_ws = worldstate.has_schema(stat_schema)
    persona_name = adventure.persona_name.strip()
    if has_ws:
        guide = worldstate.render_reference(stat_schema, persona_name)
        if guide:
            system_sections.append(Section("world_state_guide", guide))
        system_sections.append(Section("world_state_rule", worldstate.EMIT_RULE))

    if isinstance(script_mem.get("context"), str) and script_mem["context"].strip():
        system_sections.append(Section("script_context", script_mem["context"].strip()))
    if adventure.ai_instructions.strip():
        system_sections.append(Section("ai_instructions", adventure.ai_instructions.strip()))
    # Phase 18. This sits in the static block because only the user can edit it,
    # so it never changes mid-story and stays inside the cached prefix. It is
    # emitted whether or not the adventure has an RPG layer: an adventure with
    # no stats still has a protagonist, and that is the case the persona was
    # added for.
    persona_text = render_persona(adventure)
    if persona_text:
        system_sections.append(Section("persona", persona_text))
    if adventure.memory.strip():
        system_sections.append(
            Section("plot_essentials", f"Plot essentials:\n{adventure.memory.strip()}")
        )

    # ----- Live sections, which hold everything that changes -----
    # This code builds them here and places them after the history further down.
    # They are ordered from least to most volatile, so a turn that changes only
    # the fastest-moving section leaves the others cached. The summary is
    # rewritten every few turns. Lore changes with the scene. The retrieved
    # memories change on most turns, and the stat values change on nearly every
    # turn. `world_lore` is added below, because the history window determines
    # which cards trigger and that window is not known yet.
    summary_section = (
        Section("story_summary", f"Story summary:\n{adventure.story_summary.strip()}")
        if adventure.story_summary.strip()
        else None
    )
    memories_section = None
    if memory_bank and memory_bank.get("used"):
        lines_text = "\n".join(f"- {m['text']}" for m in memory_bank["used"])
        memories_section = Section("used_memories", f"Memories:\n{lines_text}")
    world_state_section = None
    refusal_note = ""
    if has_ws:
        # One read serves both the in-scene NPCs and the refusal note below.
        recent = history.tail(adventure, NPC_WINDOW, exclude_action_id)
        block = worldstate.render_state_section(
            adventure.world_state, stat_schema, _visible_npcs(recent, stat_schema),
            persona_name,
        )
        if block:
            world_state_section = Section("world_state", block)
        # Corrections for the previous AI turn only. A refusal the model has
        # already had one chance to fix is stale, and repeating it every turn
        # would price a correction into the whole rest of the adventure.
        last_ai = next((a for a in reversed(recent) if a.type == "ai"), None)
        if last_ai is not None:
            refusal_note = worldstate.render_refusals(last_ai.world_delta)

    authors_note_text = adventure.authors_note.strip()
    if isinstance(script_mem.get("authorsNote"), str) and script_mem["authorsNote"].strip():
        authors_note_text = script_mem["authorsNote"].strip()
    authors_note = f"[Author's note: {authors_note_text}]" if authors_note_text else ""

    front_memory = ""
    if isinstance(script_mem.get("frontMemory"), str):
        front_memory = script_mem["frontMemory"].strip()

    length_note = length_hint(settings.max_output_tokens, has_ws=has_ws)

    # The live sections sit below the history, but they are still part of the
    # prompt, so they still count against the budget. `world_lore` is the
    # exception, because the code below budgets it out of `available`.
    reserved = (
        sum(s.tokens for s in system_sections)
        + sum(
            s.tokens
            for s in (summary_section, memories_section, world_state_section)
            if s is not None
        )
        + count_tokens(authors_note)
        + count_tokens(front_memory)
        + count_tokens(length_note)
        + (count_tokens(worldstate.EMIT_REMINDER) if has_ws else 0)
        + count_tokens(refusal_note)
    )
    available = max(256, settings.context_token_budget - reserved)

    # Only the newest actions can reach the prompt, because the code below
    # either truncates the text to `available` tokens or stops at the budget.
    # Fetch a window that is provably larger than that and no larger. Otherwise
    # a long adventure reads its whole history on every turn and uses only the
    # end of it.
    actions = history.window_covering(
        adventure, available, count_tokens, exclude_action_id
    )

    # ----- Story cards: triggered by recent story text (the window history could fill) -----
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
    lore_section = (
        Section("world_lore", "\n".join(lore_lines)) if lore_lines else None
    )

    # ----- Story history: newest first until the remaining budget is spent -----
    history_budget = available - used
    included_actions: list[models.Action] = []
    spent = 0
    oldest_truncated = False
    for action in reversed(actions):
        # Budget against the text as it appears in the prompt, which includes
        # the state block when this adventure tracks world state.
        rendered = _history_text(action) if has_ws else action.text
        tokens = count_tokens(rendered) + count_tokens(SEPARATOR)
        if spent + tokens > history_budget:
            if not included_actions:
                # Even the newest action alone is over budget: hard-truncate it.
                included_actions.append(
                    models.Action(
                        adventure_id=action.adventure_id,
                        type=action.type,
                        text=truncate_to_last_tokens(action.text, history_budget),
                    )
                )
                oldest_truncated = True
            break
        included_actions.append(action)
        spent += tokens
    included_actions.reverse()

    # ----- Assemble the story text, with the author's note near the end -----
    # Append each AI turn's state block again. The app strips it before storage,
    # and the recent history has to show the model the pattern to follow.
    texts = [_history_text(a) if has_ws else a.text for a in included_actions]
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
    # The live sections, ordered from least to most volatile. See the comment
    # where they are built. They go below the history so that the history stays
    # cached, and above the final sections so that those stay last.
    for live in (summary_section, lore_section, memories_section, world_state_section):
        if live is not None:
            note_sections.append(live)
    if front_memory:
        note_sections.append(Section("front_memory", front_memory))
    # Place the length hint just above the emit reminder, which keeps the last
    # position. The length budget applies to the narration, and the reminder
    # applies to the block that follows it, so this is also the order in which
    # the model acts.
    note_sections.append(Section("length_hint", length_note))
    if has_ws:
        # A correction for the previous turn sits directly above the reminder
        # to emit a block, which is the instruction it modifies.
        if refusal_note:
            note_sections.append(Section("world_state_refusals", refusal_note))
        # The emit rule sits in the system block, far from where the model
        # generates text, so repeat it last where it has the most effect.
        note_sections.append(Section("world_state_reminder", worldstate.EMIT_REMINDER))

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
            # The count covers the whole story rather than the window fetched
            # above. Insights reports how many of the total actions it
            # included, so this number must be the real total.
            "total": history.count(adventure, exclude_action_id),
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
