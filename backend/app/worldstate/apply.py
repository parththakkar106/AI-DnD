"""Applying a delta or an override to world state, within the schema's limits.

Every write to world state goes through here, so the clamping rules live in one
place. A proposed change that breaks a limit is recorded as refused rather than
dropped, because the player and the model both need to see that it did not land.
"""
import copy
from typing import NamedTuple

from .schema import STAT_SECTIONS, _initials, instantiate, npc_name


class _Target(NamedTuple):
    """Where one path writes, and what the schema says about the value there."""

    kind: str                # "flag", "milestone", or "stat"
    section: str             # "flags", "milestones", "player", "world", or "npc"
    key: str                 # the key inside the container
    stat_def: dict | None    # the stat's definition, or None for a flag or milestone
    npc_id: str | None       # the character, for `npc.<id>.<stat>`, else None
    npc_stats: dict | None   # that character's stat defs, used to build a new block

    def container(self, ws: dict) -> dict:
        """Returns the dict this path writes into, and creates it if it is missing.

        Call this only when you are about to write. Creating the container is a
        side effect, and a rejected path must not leave an empty section behind.
        """
        if self.npc_id is None:
            return ws.setdefault(self.section, {})
        return ws.setdefault("npc", {}).setdefault(self.npc_id, _initials(self.npc_stats))


def _resolve(path: str, stat_schema: dict) -> tuple[_Target | None, dict | None]:
    """Routes one path to the value it names, without deciding what happens to it.

    Returns `(target, None)` when the schema defines the path, and
    `(None, rejection)` when it does not. The rejection is the entry that goes
    into a report's `rejected` list, with its reason and its fix already worded.

    `apply_delta` and `apply_override` route paths identically and differ only in
    what they write, so the routing lives here and each function keeps its own
    write rule.
    """
    parts = path.split(".")

    # `flags.<name>` is a boolean the scenario declares.
    if parts[0] == "flags" and len(parts) == 2:
        flag_defs = stat_schema.get("flags") or {}
        if parts[1] not in flag_defs:
            return None, {
                "path": path, "reason": "unknown flag",
                "fix": f"There is no flag `{parts[1]}`. {_names_phrase('flag', flag_defs)}",
            }
        return _Target("flag", "flags", parts[1], None, None, None), None

    # `milestones.<id>` records that a milestone is reached.
    if parts[0] == "milestones" and len(parts) == 2:
        milestones = stat_schema.get("milestones") or {}
        if parts[1] not in milestones:
            return None, {
                "path": path, "reason": "unknown milestone",
                "fix": f"There is no milestone `{parts[1]}`. "
                       f"{_names_phrase('milestone', milestones)}",
            }
        return _Target("milestone", "milestones", parts[1], None, None, None), None

    # `world.<stat>` and `player.<stat>`.
    if parts[0] in STAT_SECTIONS and len(parts) == 2:
        stat_defs = stat_schema.get(parts[0]) or {}
        stat_def = stat_defs.get(parts[1])
        if not isinstance(stat_def, dict):
            return None, {
                "path": path, "reason": "unknown stat",
                "fix": f"`{parts[0]}` has no stat `{parts[1]}`. "
                       f"{_names_phrase('stat', stat_defs)}",
            }
        return _Target("stat", parts[0], parts[1], stat_def, None, None), None

    # `npc.<id>.<stat>`. Each character carries its own stat definitions.
    if parts[0] == "npc" and len(parts) == 3:
        npcs = stat_schema.get("npcs") or {}
        ndef = npcs.get(parts[1])
        if not isinstance(ndef, dict):
            return None, {
                "path": path, "reason": "unknown npc",
                "fix": f"There is no character `{parts[1]}`. "
                       f"{_names_phrase('character', npcs)}",
            }
        stat_defs = ndef.get("stats") or {}
        stat_def = stat_defs.get(parts[2])
        if not isinstance(stat_def, dict):
            return None, {
                "path": path, "reason": "unknown npc stat",
                "fix": f"`{npc_name(ndef, parts[1])}` has no stat `{parts[2]}`. "
                       f"{_names_phrase('stat', stat_defs)}",
            }
        return _Target("stat", "npc", parts[2], stat_def, parts[1], stat_defs), None

    return None, {
        "path": path, "reason": "unknown path",
        "fix": f"`{path}` is not a tracked value. Use player.<stat>, "
               f"world.<stat>, npc.<id>.<stat>, flags.<name> or milestones.<id>.",
    }


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
        target, rejection = _resolve(path, stat_schema)
        if rejection is not None:
            report["rejected"].append(rejection)
            continue

        if target.kind == "flag":
            if not isinstance(value, bool):
                report["rejected"].append({
                    "path": path, "reason": "not a boolean",
                    "fix": f"`{path}` takes true or false.",
                })
                continue
            flags = target.container(ws)
            old = bool(flags.get(target.key, False))
            flags[target.key] = value
            report["applied"].append({"path": path, "old": old, "new": value})

        elif target.kind == "milestone":
            # An override toggles a milestone in both directions, so unlike a
            # delta it takes false as well as true.
            if not isinstance(value, bool):
                report["rejected"].append({
                    "path": path, "reason": "not a boolean",
                    "fix": f"`{path}` takes true or false.",
                })
                continue
            reached = target.container(ws)
            old = bool(reached.get(target.key, {}).get("reached"))
            if value:
                reached[target.key] = {"reached": True}
            else:
                reached.pop(target.key, None)
            report["applied"].append({"path": path, "old": old, "new": value})

        else:
            set_stat(target.container(ws), target.key, target.stat_def, value, path)

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

    for raw_path, change in delta.items():
        path = str(raw_path)
        target, rejection = _resolve(path, stat_schema)
        if rejection is not None:
            report["rejected"].append(rejection)
            continue

        if target.kind == "flag":
            # A flag goes both ways, so either value is accepted.
            if not isinstance(change, bool):
                report["rejected"].append({
                    "path": path, "reason": "not a boolean",
                    "fix": f"`{path}` takes true or false.",
                })
                continue
            flags = target.container(ws)
            old = bool(flags.get(target.key, False))
            if change != old:
                flags[target.key] = change
                report["applied"].append({"path": path, "old": old, "new": change})

        elif target.kind == "milestone":
            # A milestone is sticky, so only true is accepted.
            if change is not True:
                report["rejected"].append({
                    "path": path, "reason": "not true",
                    "fix": f"`{path}` can only be set to true. A milestone is "
                           f"reached once and never taken back.",
                })
                continue
            reached = target.container(ws)
            if reached.get(target.key, {}).get("reached"):
                continue  # Already reached, so do nothing.
            reached[target.key] = {"reached": True, "at": action_index}
            report["applied"].append({"path": path, "old": False, "new": True})

        elif target.stat_def.get("type") == "text":
            _apply_text_stat(target.container(ws), target.key, target.stat_def, change,
                             path, action_index, meta, report)

        else:
            _apply_stat(target.container(ws), target.key, target.stat_def, change,
                        path, action_index, meta, report)

    return ws, report
