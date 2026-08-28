"""RPG world-state engine.

The scenario carries a `stat_schema`, which is the template: which stats exist,
their bands and rules, and the milestones. An adventure carries a live
`world_state` instantiated from it. Each turn the AI proposes a delta holding
only what changed. `apply_delta` decides what the delta is allowed to do. It
clamps values to min and max, caps the change per turn, enforces cooldowns, and
makes milestones sticky.

Nothing here raises on bad AI output. A malformed delta returns `{}` and the
turn continues, the same way a broken script never breaks a turn.
"""

import copy
import json
import re

# The `stat_schema` top-level sections that hold stat definitions.
STAT_SECTIONS = ("world", "player")

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


def has_schema(stat_schema: dict | None) -> bool:
    """Returns `True` when a scenario defines an RPG layer."""
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
    """Returns the lowercased trigger words that detect an NPC in a scene.

    The words come from the NPC's `keys` field, or from its display name when
    `keys` is empty.
    """
    raw = ndef.get("keys") or npc_name(ndef, key)
    return [k.strip().lower() for k in str(raw).split(",") if k.strip()]


def _initials(defs: dict) -> dict:
    return {
        name: d.get("initial", "" if d.get("type") == "text" else 0)
        for name, d in defs.items()
        if isinstance(d, dict)
    }


def instantiate(stat_schema: dict | None) -> dict:
    """Builds a fresh live `world_state` from a schema, using initial values only."""
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


def reconcile(world_state: dict | None, stat_schema: dict | None) -> tuple[dict, dict]:
    """Brings a live `world_state` back in line with an edited schema.

    This is not `instantiate`. A value the schema still defines keeps whatever it
    reached in play, because re-instantiating would restore the player to full
    health and clear their milestones. Only the difference is applied. Stats,
    NPCs, flags, and milestones the schema gained appear at their initial value,
    and ones it no longer defines are removed along with their `last_changed`
    bookkeeping. The return value is `(new_state, report)`, where the report
    lists paths under `added` and `removed`, so the UI can show what a refresh
    would do.

    The additions are mostly cosmetic. Rendering and delta application both fall
    back to a stat definition's `initial` when the live state has no value for
    it, so a newly added stat already behaves correctly. This function stores the
    value, and unlike those read-through paths it also removes what the schema
    dropped.
    """
    report: dict = {"added": [], "removed": []}
    if not has_schema(stat_schema):
        # The scenario dropped its RPG layer, so the adventure drops it too.
        stale = bool(world_state)
        if stale:
            report["removed"].append("(all world state)")
        return {}, report

    ws = copy.deepcopy(world_state) if isinstance(world_state, dict) else {}
    if not ws:
        return instantiate(stat_schema), report

    def sync_section(container: dict, defs: dict, prefix: str) -> dict:
        out = {}
        for name, d in defs.items():
            if not isinstance(d, dict):
                continue
            if name in container:
                out[name] = container[name]
            else:
                out[name] = d.get("initial", "" if d.get("type") == "text" else 0)
                report["added"].append(f"{prefix}.{name}")
        for name in container:
            if name not in out:
                report["removed"].append(f"{prefix}.{name}")
        return out

    for section in STAT_SECTIONS:
        ws[section] = sync_section(
            ws.get(section) if isinstance(ws.get(section), dict) else {},
            stat_schema.get(section) or {},
            section,
        )

    npc_defs = stat_schema.get("npcs") or {}
    old_npcs = ws.get("npc") if isinstance(ws.get("npc"), dict) else {}
    new_npcs = {}
    for key, ndef in npc_defs.items():
        if not isinstance(ndef, dict):
            continue
        old = old_npcs.get(key) if isinstance(old_npcs.get(key), dict) else {}
        new_npcs[key] = sync_section(old, ndef.get("stats") or {}, f"npc.{key}")
    for key in old_npcs:
        if key not in new_npcs:
            report["removed"].append(f"npc.{key}")
    ws["npc"] = new_npcs

    flag_defs = stat_schema.get("flags") or {}
    old_flags = ws.get("flags") if isinstance(ws.get("flags"), dict) else {}
    new_flags = {}
    for name, d in flag_defs.items():
        if not isinstance(d, dict):
            continue
        if name in old_flags:
            new_flags[name] = bool(old_flags[name])
        else:
            new_flags[name] = bool(d.get("initial", False))
            report["added"].append(f"flags.{name}")
    for name in old_flags:
        if name not in new_flags:
            report["removed"].append(f"flags.{name}")
    ws["flags"] = new_flags

    # Milestones store only the ones reached, so there is nothing to add here.
    # An unreached milestone is absent. Drop reached milestones the scenario no
    # longer defines, because they would otherwise stay in "Achieved" with no
    # label.
    milestone_defs = stat_schema.get("milestones") or {}
    reached = ws.get("milestones") if isinstance(ws.get("milestones"), dict) else {}
    ws["milestones"] = {k: v for k, v in reached.items() if k in milestone_defs}
    for k in reached:
        if k not in milestone_defs:
            report["removed"].append(f"milestones.{k}")

    # Cooldown bookkeeping for paths that no longer exist is never read, but it
    # accumulates in every stored snapshot, so remove it with the rest.
    meta = ws.setdefault("_meta", {})
    last_changed = meta.get("last_changed")
    if isinstance(last_changed, dict):
        removed = set(report["removed"])
        meta["last_changed"] = {
            path: at for path, at in last_changed.items()
            if not any(path == r or path.startswith(f"{r}.") for r in removed)
        }
    else:
        meta["last_changed"] = {}

    return ws, report


def band_label(stat_def: dict, value) -> str | None:
    """Returns the word label for `value` from a stat definition's bands, if any.

    A band is `[lo, hi, label]` and matches when `lo <= value < hi`. The top band
    includes its upper bound, so a stat at its maximum still gets a label.
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


# --------------------------------------------------------------------------- #
# Delta application (the referee)
# --------------------------------------------------------------------------- #

def _coerce_number(value):
    if isinstance(value, bool):  # `bool` is an `int` subclass, so reject it here.
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _names_phrase(kind: str, defs: dict, limit: int = 12) -> str:
    """Lists what the model could have written instead, for a wrong name.

    A rejection that only says a name is unknown leaves the model guessing
    again. Naming the alternatives turns it into a correction it can act on.
    """
    names = [k for k in (defs or {}) if isinstance(k, str)]
    if not names:
        return f"This scenario tracks no {kind}."
    shown = ", ".join(f"`{n}`" for n in names[:limit])
    more = f", and {len(names) - limit} more" if len(names) > limit else ""
    plural = f"{kind}s" if not kind.endswith("s") else kind
    return f"The {plural} are: {shown}{more}."


def _limits_phrase(stat_def: dict) -> str:
    """Names a stat's numeric limits, for a correction sent back to the model."""
    lo, hi = stat_def.get("min"), stat_def.get("max")
    cap = stat_def.get("max_delta_per_turn")
    bits = []
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        bits.append(f"it runs from {lo} to {hi}")
    elif isinstance(lo, (int, float)):
        bits.append(f"it never goes below {lo}")
    elif isinstance(hi, (int, float)):
        bits.append(f"it never goes above {hi}")
    if isinstance(cap, (int, float)):
        bits.append(f"it moves at most {cap} per turn")
    return "; ".join(bits)


def _apply_stat(container: dict, key: str, stat_def: dict, change,
                path: str, action_index: int, meta: dict, report: dict) -> None:
    delta = _coerce_number(change)
    if delta is None:
        report["rejected"].append({
            "path": path, "reason": "not a number",
            "fix": f"`{path}` takes a number, written as a change such as -5 or 8.",
        })
        return

    cooldown = stat_def.get("cooldown") or 0
    last = meta["last_changed"].get(path)
    if cooldown and last is not None and action_index - last < cooldown:
        waited = action_index - last
        report["rejected"].append({
            "path": path, "reason": "cooldown",
            "fix": f"`{path}` changed {waited} turn(s) ago and cannot change again "
                   f"until {cooldown} turns have passed.",
        })
        return

    if stat_def.get("type") == "counter" and delta < 0:
        report["rejected"].append({
            "path": path, "reason": "counter can't decrease",
            "fix": f"`{path}` only counts up. Send a positive change such as 1, "
                   f"never a negative and never the running total.",
        })
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
    if clamped and new == old:
        # The clamp cancelled the change. Nothing moved, so the model needs the
        # same correction a rejection gets: without it the only evidence is a
        # value that stayed put, which reads as the change never being asked for.
        edge = "maximum" if hi is not None and new == hi else "minimum"
        entry["fix"] = (
            f"`{path}` did not move. It is already at its {edge} of {new}"
            + (f" ({_limits_phrase(stat_def)})." if _limits_phrase(stat_def) else ".")
        )
    report["applied"].append(entry)
    if clamped:
        report["clamped"].append(entry)


def _apply_text_stat(container: dict, key: str, stat_def: dict, change,
                     path: str, action_index: int, meta: dict, report: dict) -> None:
    """Applies a free-text stat, which replaces rather than adds.

    The AI sends the new value in full rather than a delta. No clamping and no
    bands apply. Only an optional cooldown and an optional `max_length`
    truncation apply.
    """
    if not isinstance(change, str):
        report["rejected"].append({
            "path": path, "reason": "not a string",
            "fix": f"`{path}` holds text. Send its new value in full, not a number "
                   f"and not a change.",
        })
        return

    cooldown = stat_def.get("cooldown") or 0
    last = meta["last_changed"].get(path)
    if cooldown and last is not None and action_index - last < cooldown:
        waited = action_index - last
        report["rejected"].append({
            "path": path, "reason": "cooldown",
            "fix": f"`{path}` changed {waited} turn(s) ago and cannot change again "
                   f"until {cooldown} turns have passed.",
        })
        return

    new = change.strip()
    max_len = stat_def.get("max_length")
    if isinstance(max_len, int) and max_len > 0 and len(new) > max_len:
        new = new[:max_len]

    old = container.get(key, stat_def.get("initial", ""))
    if new == old:
        return  # Nothing changed, so do nothing.

    container[key] = new
    meta["last_changed"][path] = action_index
    report["applied"].append({"path": path, "old": old, "new": new})


def apply_override(world_state: dict, stat_schema: dict, overrides: dict) -> tuple[dict, dict]:
    """Sets live values directly, as a manual author edit rather than an AI turn.

    This differs from `apply_delta` in three ways. Numeric stats are set rather
    than added to. `cooldown`, `max_delta_per_turn`, and the rule that a counter
    cannot decrease are all ignored, because this is a deliberate correction
    rather than an AI move to check. Milestones can be toggled in both
    directions rather than only marked reached.

    Values are still validated against the schema, so an unknown path or a wrong
    type is rejected, and numeric values still clamp to min and max.
    """
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
                report["rejected"].append({
                    "path": path, "reason": "unknown stat",
                    "fix": f"`{parts[0]}` has no stat `{parts[1]}`. "
                           f"{_names_phrase('stat', stat_schema.get(parts[0]) or {})}",
                })
                continue
            container = ws.setdefault(parts[0], {})
            set_stat(container, parts[1], stat_def, value, path)
            continue

        if parts[0] == "npc" and len(parts) == 3:
            ndef = npcs.get(parts[1])
            if not isinstance(ndef, dict):
                report["rejected"].append({
                    "path": path, "reason": "unknown npc",
                    "fix": f"There is no character `{parts[1]}`. "
                           f"{_names_phrase('character', npcs)}",
                })
                continue
            stat_defs = ndef.get("stats") or {}
            stat_def = stat_defs.get(parts[2])
            if not isinstance(stat_def, dict):
                report["rejected"].append({
                    "path": path, "reason": "unknown npc stat",
                    "fix": f"`{npc_name(ndef, parts[1])}` has no stat `{parts[2]}`. "
                           f"{_names_phrase('stat', stat_defs)}",
                })
                continue
            npc_state = ws.setdefault("npc", {})
            container = npc_state.setdefault(parts[1], _initials(stat_defs))
            set_stat(container, parts[2], stat_def, value, path)
            continue

        report["rejected"].append({
            "path": path, "reason": "unknown path",
            "fix": f"`{path}` is not a tracked value. Use player.<stat>, "
                   f"world.<stat>, npc.<id>.<stat>, flags.<name> or milestones.<id>.",
        })

    return ws, report


def apply_delta(world_state: dict, stat_schema: dict, delta: dict,
                action_index: int) -> tuple[dict, dict]:
    """Validates and clamps `delta` against `stat_schema`, then applies it.

    The delta is applied to a copy of `world_state`. The return value is
    `(new_world_state, report)`.
    """
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

        # `flags.<name>` is a two-way boolean, and either value is accepted.
        if parts[0] == "flags" and len(parts) == 2:
            fid = parts[1]
            if fid not in flag_defs:
                report["rejected"].append({
                    "path": path, "reason": "unknown flag",
                    "fix": f"There is no flag `{fid}`. {_names_phrase('flag', flag_defs)}",
                })
                continue
            if not isinstance(change, bool):
                report["rejected"].append({
                    "path": path, "reason": "not a boolean",
                    "fix": f"`{path}` takes true or false.",
                })
                continue
            flags = ws.setdefault("flags", {})
            old = bool(flags.get(fid, False))
            if change != old:
                flags[fid] = change
                report["applied"].append({"path": path, "old": old, "new": change})
            continue

        # `milestones.<id>` is a sticky boolean, and only `true` is accepted.
        if parts[0] == "milestones" and len(parts) == 2:
            mid = parts[1]
            if mid not in milestones:
                report["rejected"].append({
                    "path": path, "reason": "unknown milestone",
                    "fix": f"There is no milestone `{mid}`. "
                           f"{_names_phrase('milestone', milestones)}",
                })
                continue
            if change is not True:
                report["rejected"].append({
                    "path": path, "reason": "not true",
                    "fix": f"`{path}` can only be set to true. A milestone is "
                           f"reached once and never taken back.",
                })
                continue
            reached = ws.setdefault("milestones", {})
            if reached.get(mid, {}).get("reached"):
                continue  # Already reached, so do nothing.
            reached[mid] = {"reached": True, "at": action_index}
            report["applied"].append({"path": path, "old": False, "new": True})
            continue

        # world.<stat> / player.<stat>
        if parts[0] in STAT_SECTIONS and len(parts) == 2:
            stat_def = (stat_schema.get(parts[0]) or {}).get(parts[1])
            if not isinstance(stat_def, dict):
                report["rejected"].append({
                    "path": path, "reason": "unknown stat",
                    "fix": f"`{parts[0]}` has no stat `{parts[1]}`. "
                           f"{_names_phrase('stat', stat_schema.get(parts[0]) or {})}",
                })
                continue
            container = ws.setdefault(parts[0], {})
            if stat_def.get("type") == "text":
                _apply_text_stat(container, parts[1], stat_def, change, path,
                                 action_index, meta, report)
            else:
                _apply_stat(container, parts[1], stat_def, change, path,
                            action_index, meta, report)
            continue

        # `npc.<npcId>.<stat>`. Each NPC has its own stat definitions.
        if parts[0] == "npc" and len(parts) == 3:
            ndef = npcs.get(parts[1])
            if not isinstance(ndef, dict):
                report["rejected"].append({
                    "path": path, "reason": "unknown npc",
                    "fix": f"There is no character `{parts[1]}`. "
                           f"{_names_phrase('character', npcs)}",
                })
                continue
            stat_defs = ndef.get("stats") or {}
            stat_def = stat_defs.get(parts[2])
            if not isinstance(stat_def, dict):
                report["rejected"].append({
                    "path": path, "reason": "unknown npc stat",
                    "fix": f"`{npc_name(ndef, parts[1])}` has no stat `{parts[2]}`. "
                           f"{_names_phrase('stat', stat_defs)}",
                })
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

        report["rejected"].append({
            "path": path, "reason": "unknown path",
            "fix": f"`{path}` is not a tracked value. Use player.<stat>, "
                   f"world.<stat>, npc.<id>.<stat>, flags.<name> or milestones.<id>.",
        })

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
    """Returns the compact context block that every turn includes.

    `visible_npcs` maps a card id to a display name, for the NPCs currently in
    the scene.
    """
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

    # Show the id beside each goal, the same as NPCs and flags. The AI marks a
    # milestone as `milestones.<id>`, and `apply_delta` rejects an id the schema
    # does not define, so a goal listed by description alone gives the model no
    # way to name it and it can only guess.
    milestones = stat_schema.get("milestones") or {}
    reached = ws.get("milestones") or {}
    goals = [f"{mid} — {d.get('desc', mid)}" for mid, d in milestones.items()
             if not reached.get(mid, {}).get("reached")]
    done = [f"{mid} — {d.get('desc', mid)}" for mid, d in milestones.items()
            if reached.get(mid, {}).get("reached")]
    if goals:
        lines.append("Goals (mark with milestones.<id>): " + "; ".join(goals) + ".")
    if done:
        lines.append("Achieved: " + "; ".join(done) + ".")

    return "\n".join(lines)


def _describe_stat(name: str, d: dict) -> str | None:
    """Returns one reference line for a stat.

    The description and the band ladder are independent, and each is included
    only when present, so a stat may have either, both, or neither.
    """
    bits: list[str] = []
    desc = d.get("desc")
    if isinstance(desc, str) and desc.strip():
        # Fragments are joined with "; " and end with a single ".", so remove
        # any trailing period the author put on the description.
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
    """Returns a fixed, per-scenario legend for the stats.

    Each line gives what a stat means, from its `desc`, and its band ladder. The
    legend does not change from turn to turn, and it is separate from the live
    values.
    """
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
