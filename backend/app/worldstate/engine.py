"""RPG world-state engine.

The scenario carries a `stat_schema` (the template: which stats exist, their
bands and rules, and the milestones). An adventure carries a live `world_state`
instantiated from it. Each turn the AI proposes a *delta* (only what changed);
`apply_delta` is the referee — it clamps to min/max, caps per-turn change,
enforces cooldowns, and marks milestones sticky.

Nothing here ever raises on bad AI output: a malformed delta yields `{}` and the
turn continues, exactly like a broken script never breaks a turn.
"""

import copy
import json
import re

# stat_schema top-level sections that hold stat definitions.
STAT_SECTIONS = ("world", "player")

# Appended once to the system prompt so the model knows how to report changes.
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
    'its range in a single ordinary turn. Use the paths exactly as shown in the world state: '
    '"player.<stat>", '
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

# ```state { ... } ``` (also tolerates ```json or an unlabelled fence); DOTALL.
_FENCE_RE = re.compile(r"```(?:state|json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
# Fallback: a bare JSON object hugging the end of the text.
_TRAILING_RE = re.compile(r"(\{[^{}]*\})\s*$", re.DOTALL)


def has_schema(stat_schema: dict | None) -> bool:
    """True when a scenario actually defines an RPG layer."""
    if not isinstance(stat_schema, dict):
        return False
    return any(
        isinstance(stat_schema.get(k), dict) and stat_schema[k]
        for k in (*STAT_SECTIONS, "npcs", "milestones", "flags")
    )


def npc_name(ndef: dict, key: str) -> str:
    name = ndef.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else key


def npc_triggers(ndef: dict, key: str) -> list[str]:
    """Lower-cased trigger words for detecting an NPC in scene: its `keys`
    field, falling back to its display name."""
    raw = ndef.get("keys") or npc_name(ndef, key)
    return [k.strip().lower() for k in str(raw).split(",") if k.strip()]


def _initials(defs: dict) -> dict:
    return {
        name: d.get("initial", "" if d.get("type") == "text" else 0)
        for name, d in defs.items()
        if isinstance(d, dict)
    }


def instantiate(stat_schema: dict | None) -> dict:
    """Build a fresh live world_state from a schema (initial values only)."""
    if not has_schema(stat_schema):
        return {}
    ws: dict = {}
    for section in STAT_SECTIONS:
        ws[section] = _initials(stat_schema.get(section) or {})
    # Each defined NPC gets its own stat block from its own `stats` defs.
    ws["npc"] = {
        key: _initials(ndef.get("stats") or {})
        for key, ndef in (stat_schema.get("npcs") or {}).items()
        if isinstance(ndef, dict)
    }
    ws["milestones"] = {}   # only reached ones are stored
    ws["flags"] = {
        name: bool(d.get("initial", False))
        for name, d in (stat_schema.get("flags") or {}).items()
        if isinstance(d, dict)
    }
    ws["_meta"] = {"last_changed": {}}
    return ws


def band_label(stat_def: dict, value) -> str | None:
    """The word label for `value` from a stat def's bands, if any.

    Bands are [lo, hi, label]; matched as lo <= value < hi, with the top band
    inclusive of its upper bound so a maxed stat still gets a label.
    """
    bands = stat_def.get("bands")
    if not isinstance(bands, list) or not isinstance(value, (int, float)):
        return None
    last_hi = None
    for band in bands:
        if not (isinstance(band, list) and len(band) == 3):
            continue
        lo, hi, label = band
        last_hi = hi
        if lo <= value < hi:
            return str(label)
    # Inclusive top edge.
    if bands and value == last_hi:
        return str(bands[-1][2])
    return None


# --------------------------------------------------------------------------- #
# Delta extraction
# --------------------------------------------------------------------------- #

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
    """Pull the trailing state block out of an AI response.

    Returns (clean_text, delta). `delta` is `{}` when there is no block or it
    can't be parsed; `clean_text` has the block removed. Only strips a bare
    trailing object when it actually parses to a delta, so ordinary prose
    ending in `}` is never eaten.
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


# --------------------------------------------------------------------------- #
# Delta application (the referee)
# --------------------------------------------------------------------------- #

def _coerce_number(value):
    if isinstance(value, bool):  # bool is an int subclass — reject here
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _apply_stat(container: dict, key: str, stat_def: dict, change,
                path: str, action_index: int, meta: dict, report: dict) -> None:
    delta = _coerce_number(change)
    if delta is None:
        report["rejected"].append({"path": path, "reason": "not a number"})
        return

    cooldown = stat_def.get("cooldown") or 0
    last = meta["last_changed"].get(path)
    if cooldown and last is not None and action_index - last < cooldown:
        report["rejected"].append({"path": path, "reason": "cooldown"})
        return

    if stat_def.get("type") == "counter" and delta < 0:
        report["rejected"].append({"path": path, "reason": "counter can't decrease"})
        return

    clamped = False
    cap = stat_def.get("max_delta_per_turn")
    if cap is not None and abs(delta) > cap:
        delta = cap if delta > 0 else -cap
        clamped = True

    old = container.get(key, stat_def.get("initial", 0))
    new = old + delta
    lo, hi = stat_def.get("min"), stat_def.get("max")
    if lo is not None and new < lo:
        new, clamped = lo, True
    if hi is not None and new > hi:
        new, clamped = hi, True
    # Keep ints integral for display.
    if isinstance(old, int) and float(new).is_integer():
        new = int(new)

    container[key] = new
    meta["last_changed"][path] = action_index
    entry = {"path": path, "old": old, "new": new}
    report["applied"].append(entry)
    if clamped:
        report["clamped"].append(entry)


def _apply_text_stat(container: dict, key: str, stat_def: dict, change,
                     path: str, action_index: int, meta: dict, report: dict) -> None:
    """Free-text stats replace rather than add: the AI sends the new value in
    full, not a delta. No clamping/bands apply — only an optional cooldown and
    an optional max_length truncation."""
    if not isinstance(change, str):
        report["rejected"].append({"path": path, "reason": "not a string"})
        return

    cooldown = stat_def.get("cooldown") or 0
    last = meta["last_changed"].get(path)
    if cooldown and last is not None and action_index - last < cooldown:
        report["rejected"].append({"path": path, "reason": "cooldown"})
        return

    new = change.strip()
    max_len = stat_def.get("max_length")
    if isinstance(max_len, int) and max_len > 0 and len(new) > max_len:
        new = new[:max_len]

    old = container.get(key, stat_def.get("initial", ""))
    if new == old:
        return  # no actual change — silent no-op

    container[key] = new
    meta["last_changed"][path] = action_index
    report["applied"].append({"path": path, "old": old, "new": new})


def apply_override(world_state: dict, stat_schema: dict, overrides: dict) -> tuple[dict, dict]:
    """Directly set live values — a manual author/admin edit, not an AI turn.

    Unlike `apply_delta`: numeric stats are SET rather than added to, and
    `cooldown`/`max_delta_per_turn`/the counter-can't-decrease rule are all
    ignored (a deliberate correction, not an AI move to police). Milestones
    can be toggled either way, not only marked reached. Values are still
    validated against the schema (unknown path/type is rejected) and numeric
    values still clamp to min/max."""
    ws = copy.deepcopy(world_state) if isinstance(world_state, dict) else {}
    if not ws:
        ws = instantiate(stat_schema)
    report: dict = {"applied": [], "rejected": []}

    if not isinstance(overrides, dict):
        return ws, report

    milestones = stat_schema.get("milestones") or {}
    flag_defs = stat_schema.get("flags") or {}
    npcs = stat_schema.get("npcs") or {}

    def set_stat(container: dict, key: str, stat_def: dict, value, path: str) -> None:
        if stat_def.get("type") == "text":
            if not isinstance(value, str):
                report["rejected"].append({"path": path, "reason": "not a string"})
                return
            new = value.strip()
            max_len = stat_def.get("max_length")
            if isinstance(max_len, int) and max_len > 0 and len(new) > max_len:
                new = new[:max_len]
            old = container.get(key, stat_def.get("initial", ""))
            container[key] = new
            report["applied"].append({"path": path, "old": old, "new": new})
            return

        num = _coerce_number(value)
        if num is None:
            report["rejected"].append({"path": path, "reason": "not a number"})
            return
        old = container.get(key, stat_def.get("initial", 0))
        lo, hi = stat_def.get("min"), stat_def.get("max")
        if lo is not None and num < lo:
            num = lo
        if hi is not None and num > hi:
            num = hi
        if isinstance(old, int) and float(num).is_integer():
            num = int(num)
        container[key] = num
        report["applied"].append({"path": path, "old": old, "new": num})

    for raw_path, value in overrides.items():
        path = str(raw_path)
        parts = path.split(".")

        if parts[0] == "flags" and len(parts) == 2:
            fid = parts[1]
            if fid not in flag_defs:
                report["rejected"].append({"path": path, "reason": "unknown flag"})
                continue
            if not isinstance(value, bool):
                report["rejected"].append({"path": path, "reason": "not a boolean"})
                continue
            flags = ws.setdefault("flags", {})
            old = bool(flags.get(fid, False))
            flags[fid] = value
            report["applied"].append({"path": path, "old": old, "new": value})
            continue

        if parts[0] == "milestones" and len(parts) == 2:
            mid = parts[1]
            if mid not in milestones:
                report["rejected"].append({"path": path, "reason": "unknown milestone"})
                continue
            if not isinstance(value, bool):
                report["rejected"].append({"path": path, "reason": "not a boolean"})
                continue
            reached = ws.setdefault("milestones", {})
            old = bool(reached.get(mid, {}).get("reached"))
            if value:
                reached[mid] = {"reached": True}
            else:
                reached.pop(mid, None)
            report["applied"].append({"path": path, "old": old, "new": value})
            continue

        if parts[0] in STAT_SECTIONS and len(parts) == 2:
            stat_def = (stat_schema.get(parts[0]) or {}).get(parts[1])
            if not isinstance(stat_def, dict):
                report["rejected"].append({"path": path, "reason": "unknown stat"})
                continue
            container = ws.setdefault(parts[0], {})
            set_stat(container, parts[1], stat_def, value, path)
            continue

        if parts[0] == "npc" and len(parts) == 3:
            ndef = npcs.get(parts[1])
            if not isinstance(ndef, dict):
                report["rejected"].append({"path": path, "reason": "unknown npc"})
                continue
            stat_defs = ndef.get("stats") or {}
            stat_def = stat_defs.get(parts[2])
            if not isinstance(stat_def, dict):
                report["rejected"].append({"path": path, "reason": "unknown npc stat"})
                continue
            npc_state = ws.setdefault("npc", {})
            container = npc_state.setdefault(parts[1], _initials(stat_defs))
            set_stat(container, parts[2], stat_def, value, path)
            continue

        report["rejected"].append({"path": path, "reason": "unknown path"})

    return ws, report


def apply_delta(world_state: dict, stat_schema: dict, delta: dict,
                action_index: int) -> tuple[dict, dict]:
    """Validate/clamp `delta` against `stat_schema` and apply to a copy of
    `world_state`. Returns (new_world_state, report)."""
    ws = copy.deepcopy(world_state) if isinstance(world_state, dict) else {}
    if not ws:
        ws = instantiate(stat_schema)
    ws.setdefault("_meta", {}).setdefault("last_changed", {})
    meta = ws["_meta"]
    report: dict = {"applied": [], "clamped": [], "rejected": []}

    if not isinstance(delta, dict):
        return ws, report

    milestones = stat_schema.get("milestones") or {}
    flag_defs = stat_schema.get("flags") or {}
    npcs = stat_schema.get("npcs") or {}

    for raw_path, change in delta.items():
        path = str(raw_path)
        parts = path.split(".")

        # flags.<name>  — free two-way boolean, either value accepted.
        if parts[0] == "flags" and len(parts) == 2:
            fid = parts[1]
            if fid not in flag_defs:
                report["rejected"].append({"path": path, "reason": "unknown flag"})
                continue
            if not isinstance(change, bool):
                report["rejected"].append({"path": path, "reason": "not a boolean"})
                continue
            flags = ws.setdefault("flags", {})
            old = bool(flags.get(fid, False))
            if change != old:
                flags[fid] = change
                report["applied"].append({"path": path, "old": old, "new": change})
            continue

        # milestones.<id>  — sticky boolean, only `true` accepted.
        if parts[0] == "milestones" and len(parts) == 2:
            mid = parts[1]
            if mid not in milestones:
                report["rejected"].append({"path": path, "reason": "unknown milestone"})
                continue
            if change is not True:
                report["rejected"].append({"path": path, "reason": "not true"})
                continue
            reached = ws.setdefault("milestones", {})
            if reached.get(mid, {}).get("reached"):
                continue  # already done — silent no-op
            reached[mid] = {"reached": True, "at": action_index}
            report["applied"].append({"path": path, "old": False, "new": True})
            continue

        # world.<stat> / player.<stat>
        if parts[0] in STAT_SECTIONS and len(parts) == 2:
            stat_def = (stat_schema.get(parts[0]) or {}).get(parts[1])
            if not isinstance(stat_def, dict):
                report["rejected"].append({"path": path, "reason": "unknown stat"})
                continue
            container = ws.setdefault(parts[0], {})
            if stat_def.get("type") == "text":
                _apply_text_stat(container, parts[1], stat_def, change, path,
                                 action_index, meta, report)
            else:
                _apply_stat(container, parts[1], stat_def, change, path,
                            action_index, meta, report)
            continue

        # npc.<npcId>.<stat>  — each NPC has its own stat defs.
        if parts[0] == "npc" and len(parts) == 3:
            ndef = npcs.get(parts[1])
            if not isinstance(ndef, dict):
                report["rejected"].append({"path": path, "reason": "unknown npc"})
                continue
            stat_defs = ndef.get("stats") or {}
            stat_def = stat_defs.get(parts[2])
            if not isinstance(stat_def, dict):
                report["rejected"].append({"path": path, "reason": "unknown npc stat"})
                continue
            npc_state = ws.setdefault("npc", {})
            container = npc_state.setdefault(parts[1], _initials(stat_defs))
            if stat_def.get("type") == "text":
                _apply_text_stat(container, parts[2], stat_def, change, path,
                                 action_index, meta, report)
            else:
                _apply_stat(container, parts[2], stat_def, change, path,
                            action_index, meta, report)
            continue

        report["rejected"].append({"path": path, "reason": "unknown path"})

    return ws, report


# --------------------------------------------------------------------------- #
# Context rendering
# --------------------------------------------------------------------------- #

def _stat_line(defs: dict, values: dict) -> str:
    parts = []
    for name, d in defs.items():
        if not isinstance(d, dict):
            continue
        if d.get("type") == "text":
            val = values.get(name, d.get("initial", ""))
            parts.append(f'{name} "{val}"' if val else f"{name} (unset)")
            continue
        val = values.get(name, d.get("initial", 0))
        hi = d.get("max")
        shown = f"{val}/{hi}" if hi is not None else f"{val}"
        label = band_label(d, val)
        parts.append(f"{name} {shown}" + (f" ({label})" if label else ""))
    return ", ".join(parts)


def render_state_section(world_state: dict, stat_schema: dict,
                         visible_npcs: dict[str, str]) -> str:
    """Compact, always-included context block. `visible_npcs` maps card-id ->
    display name for NPCs currently in scene."""
    ws = world_state if isinstance(world_state, dict) else {}
    lines: list[str] = []

    world_defs = stat_schema.get("world") or {}
    world_line = _stat_line(world_defs, ws.get("world") or {})
    header = "World state" + (f" — {world_line}." if world_line else ".")
    lines.append(header)

    player_defs = stat_schema.get("player") or {}
    player_line = _stat_line(player_defs, ws.get("player") or {})
    if player_line:
        lines.append(f"You: {player_line}.")

    npcs = stat_schema.get("npcs") or {}
    npc_state = ws.get("npc") or {}
    for npc_key, name in visible_npcs.items():
        ndef = npcs.get(npc_key) or {}
        stat_defs = ndef.get("stats") or {}
        values = npc_state.get(npc_key) or _initials(stat_defs)
        npc_line = _stat_line(stat_defs, values)
        if npc_line:
            # Show the id so the AI can address it as npc.<id>.<stat>.
            lines.append(f"{name} (npc.{npc_key}): {npc_line}.")

    flag_defs = stat_schema.get("flags") or {}
    flag_state = ws.get("flags") or {}
    flag_parts = [
        f"{name} {'yes' if flag_state.get(name, bool(d.get('initial', False))) else 'no'}"
        for name, d in flag_defs.items() if isinstance(d, dict)
    ]
    if flag_parts:
        lines.append("Flags: " + ", ".join(flag_parts) + ".")

    milestones = stat_schema.get("milestones") or {}
    reached = ws.get("milestones") or {}
    goals = [d.get("desc", mid) for mid, d in milestones.items()
             if not reached.get(mid, {}).get("reached")]
    done = [d.get("desc", mid) for mid, d in milestones.items()
            if reached.get(mid, {}).get("reached")]
    if goals:
        lines.append("Goals: " + "; ".join(goals) + ".")
    if done:
        lines.append("Achieved: " + "; ".join(done) + ".")

    return "\n".join(lines)


def _describe_stat(name: str, d: dict) -> str | None:
    """One reference line for a stat. Description and band-ladder are independent —
    each is included only when present, so a stat may have either, both, or neither."""
    bits: list[str] = []
    desc = d.get("desc")
    if isinstance(desc, str) and desc.strip():
        # Fragments are joined with "; " and end with a single ".", so drop any
        # trailing period the author already put on the description.
        bits.append(desc.strip().rstrip("."))
    if d.get("type") == "text":
        bits.append("free text")
        return f"{name} — {'; '.join(bits)}." if bits else None
    lo, hi = d.get("min"), d.get("max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        bits.append(f"range {lo}–{hi}")
    bands = d.get("bands")
    if isinstance(bands, list) and bands:
        ladder = ", ".join(
            f"{b[0]}–{b[1]} {b[2]}"
            for b in bands if isinstance(b, list) and len(b) == 3
        )
        if ladder:
            bits.append(f"bands: {ladder}")
    return f"{name} — {'; '.join(bits)}." if bits else None


def render_reference(stat_schema: dict) -> str:
    """A fixed, per-scenario legend describing what each stat means (its `desc`)
    and its band ladder. Static across turns — separate from the live values."""
    lines: list[str] = []
    for section in STAT_SECTIONS:
        for name, d in (stat_schema.get(section) or {}).items():
            if isinstance(d, dict):
                row = _describe_stat(name, d)
                if row:
                    lines.append(row)
    for npc_key, ndef in (stat_schema.get("npcs") or {}).items():
        if not isinstance(ndef, dict):
            continue
        name = npc_name(ndef, npc_key)
        desc = ndef.get("desc")
        if isinstance(desc, str) and desc.strip():
            lines.append(f"NPC {name} ({npc_key}) — {desc.strip().rstrip('.')}.")
        for sname, sdef in (ndef.get("stats") or {}).items():
            if isinstance(sdef, dict):
                row = _describe_stat(f"{name} {sname}", sdef)
                if row:
                    lines.append(row)
    for name, d in (stat_schema.get("flags") or {}).items():
        if isinstance(d, dict):
            desc = d.get("desc")
            if isinstance(desc, str) and desc.strip():
                lines.append(f"{name} (flag) — {desc.strip().rstrip('.')}.")
    if not lines:
        return ""
    return "Stat guide (fixed reference):\n" + "\n".join(f"- {ln}" for ln in lines)
