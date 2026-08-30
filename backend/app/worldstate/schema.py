"""Reading a scenario's `stat_schema` and building state that matches it.

Nothing here decides what a turn changes. These functions answer what the schema
allows, what a fresh state looks like, and how an edited schema maps onto state
that already exists.
"""
import copy


# The `stat_schema` top-level sections that hold stat definitions.
STAT_SECTIONS = ("world", "player")


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
