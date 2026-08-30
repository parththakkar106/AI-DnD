"""The delta block: the rule the model is given, and reading back what it sent.

The model writes one fenced block per turn. This module holds the instruction
text, the parser that survives the ways a model gets the format wrong, and the
report rendering that tells a player which changes were refused.
"""
import json
import re


# Appended once to the system prompt, so the model knows how to report
# changes.
EMIT_RULE = (
    "You maintain a numeric world state. Treat your own narration as authoritative: "
    "whenever what you write implies a change to any tracked value — health or resources "
    "going up or down, time passing, a relationship or mood shifting, a status turning on "
    "or off, progress toward a goal, an item or piece of information gained or lost — you "
    "MUST record it, including the numbers, not just on/off flags. After your narration, "
    "append a fenced code block labelled `state` with a JSON object of the CHANGES ONLY, "
    "as deltas (not new totals). Update every value the scene affected this turn, not only "
    "the obvious ones. Read the range and band labels shown for each stat and keep every "
    "change proportionate to the moment: an ordinary or minor event nudges a value slightly, "
    "while a large change — or reaching a stat's minimum or maximum — is reserved for a "
    "genuinely pivotal, defining moment (a passing remark shifts a relationship a little; a "
    "lasting act of loyalty or betrayal shifts it a lot). Do not move a value across most of "
    'its range in a single ordinary turn. Every stat is listed by its exact path in the '
    'stat guide and again beside its live value — copy a path from there rather than '
    'building one out of a name. The shapes are "player.<stat>", '
    '"world.<stat>", "npc.<id>.<stat>" (use the id in parentheses, e.g. npc.gwen.trust, '
    'not the display name); "flags.<name>": true or false to toggle an on/off state; and '
    '"milestones.<id>": true when an objective is completed. Some stats marked (free text) in '
    "the stat guide hold a short string instead of a number — for those, send the new value "
    "in full (not a delta), e.g. what the player is now wearing or holding; only send it when "
    "it actually changed. Send only things that actually "
    "changed and never restate unchanged values; if truly nothing changed, omit the block. "
    "Example:\n"
    '```state\n{"player.hp": -15, "npc.gwen.trust": 5, "milestones.escaped": true, '
    '"player.outfit": "torn traveling cloak"}\n```'
)


# A short reminder placed at the end of the prompt, which is the strongest
# recency position, so the emit rule is close to where the model generates.
EMIT_REMINDER = (
    "[Reminder: end your reply with a ```state block of the changes this turn "
    "(deltas only), or omit it if truly nothing changed.]"
)


def render_delta_block(delta: dict) -> str:
    """Renders a stored delta back into the fenced `state` block the AI emitted.

    The caller replays past turns into the context with this, so the model copies
    the format. An empty delta returns an empty string, which means the turn
    changed nothing.
    """
    if not isinstance(delta, dict) or not delta:
        return ""
    return "```state\n" + json.dumps(delta, ensure_ascii=False) + "\n```"


def applied_delta(world_delta: dict | None) -> dict:
    """Returns the changes the engine accepted, shaped as the AI sends them.

    Replaying the delta the AI sent would show it a refused change standing as
    though it had been applied, contradicted by the live values in the same
    prompt. The model has no way to read that as a correction, so it repeats
    the change. Replaying what was accepted removes the contradiction.

    A numeric change that ended where it started is omitted, because it moved
    nothing and a zero in the replayed block reads as a value worth sending.
    """
    if not isinstance(world_delta, dict):
        return {}
    out: dict = {}
    for entry in world_delta.get("applied") or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        old, new = entry.get("old"), entry.get("new")
        if path.startswith("milestones."):
            out[path] = True
        elif path.startswith("flags."):
            out[path] = bool(new)
        elif isinstance(old, (int, float)) and isinstance(new, (int, float)):
            if new != old:
                out[path] = new - old
        else:
            out[path] = new
    return out


def refusals(world_delta: dict | None) -> list[str]:
    """Returns a correction line for each change the engine did not carry out.

    Covers the changes that were lost: a rejection, and a clamp that left the
    value where it started. A clamp that reduced a change but still moved the
    value is deliberately absent. The model's intent landed in that case, and
    reporting the shortfall invites it to send the remainder on the next turn,
    which is the swing `max_delta_per_turn` exists to prevent.
    """
    if not isinstance(world_delta, dict):
        return []
    lines = []
    for entry in world_delta.get("rejected") or []:
        if isinstance(entry, dict) and entry.get("fix"):
            lines.append(str(entry["fix"]))
    for entry in world_delta.get("clamped") or []:
        if isinstance(entry, dict) and entry.get("fix"):
            lines.append(str(entry["fix"]))
    return lines


def render_refusals(world_delta: dict | None) -> str:
    """Renders `refusals` as the note appended after the most recent AI turn."""
    lines = refusals(world_delta)
    if not lines:
        return ""
    body = "\n".join(f"- {ln}" for ln in lines)
    return ("[Part of your last state block was not applied. Correct it in this "
            f"turn's block:\n{body}]")


# ```state { ... } ``` (also tolerates ```json or an unlabelled fence); DOTALL.
_FENCE_RE = re.compile(r"```(?:state|json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


# Fallback: a bare JSON object hugging the end of the text.
_TRAILING_RE = re.compile(r"(\{[^{}]*\})\s*$", re.DOTALL)


def _tolerant_load(blob: str) -> dict:
    # Strip trailing commas and leading + on numbers, both of which weaker
    # free models emit and strict JSON rejects.
    cleaned = re.sub(r",(\s*[}\]])", r"\1", blob)
    cleaned = re.sub(r"(:\s*)\+(\d)", r"\1\2", cleaned)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_delta(text: str) -> tuple[str, dict]:
    """Removes the trailing state block from an AI response.

    The return value is `(clean_text, delta)`. `delta` is `{}` when there is no
    block or the block cannot be parsed, and `clean_text` has the block removed.
    A bare trailing object is stripped only when it parses to a delta, so
    ordinary prose that ends in `}` is left alone.
    """
    matches = list(_FENCE_RE.finditer(text))
    if matches:
        m = matches[-1]
        delta = _tolerant_load(m.group(1))
        clean = (text[: m.start()] + text[m.end():]).strip()
        return clean, delta

    m = _TRAILING_RE.search(text)
    if m:
        delta = _tolerant_load(m.group(1))
        if delta and all("." in str(k) for k in delta):
            clean = text[: m.start()].strip()
            return clean, delta
    return text.strip(), {}
