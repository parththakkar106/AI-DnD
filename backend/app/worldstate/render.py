"""Turning world state into the text the model and the player read.

`render_state_section` writes the current state into the prompt.
`render_reference` writes the schema itself, so the model knows which stats
exist and what they are called.
"""
from .schema import STAT_SECTIONS, _initials, band_label, npc_name


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
                         visible_npcs: dict[str, str],
                         player_name: str = "") -> str:
    """Returns the compact context block that every turn includes.

    `visible_npcs` maps a card id to a display name, for the NPCs currently in
    the scene.

    `player_name` is the persona's name (Phase 18), used to label the player's
    stat block. It is empty for an adventure with no persona, and the block then
    reads `You:` as it did before personas existed.
    """
    ws = world_state if isinstance(world_state, dict) else {}
    lines: list[str] = []

    # The header names these as the running totals, and names the block the
    # model writes as the movements. The two used to be "World state" and a
    # ```state block, and a model reading its own last block back in the history
    # took it for the state, then wrote totals into the next one. Whatever this
    # header says, say the same thing in `parse.EMIT_RULE`, which points at it
    # by name.
    lines.append("Current world state (running totals; report changes to these "
                 "in your state_delta block):")

    world_defs = stat_schema.get("world") or {}
    world_line = _stat_line(world_defs, ws.get("world") or {})
    if world_line:
        lines.append(f"World: {world_line}.")

    player_defs = stat_schema.get("player") or {}
    player_line = _stat_line(player_defs, ws.get("player") or {})
    if player_line:
        # Name the block after the persona, and show the path beside it exactly
        # as the NPC lines below do. The model reads the name in the narration,
        # so without the path in view it writes `kaelen.hp` and the delta is
        # refused as an unknown path.
        label = f"{player_name} (player)" if player_name else "You"
        lines.append(f"{label}: {player_line}.")

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
    """Returns one reference line for a stat, named by the path the AI writes.

    `name` is that path. The guide is the model's only complete list of what
    exists, so a stat named any other way leaves it to work the path out from
    the live values, and those cover only what is on screen this turn.

    The description and the band ladder are independent, and each is included
    only when present, so a stat may have either, both, or neither.
    """
    is_text = d.get("type") == "text"
    # `EMIT_RULE` sends the model here to find out which stats take a whole
    # value instead of a change, and it names this marker, so the two have to
    # be written the same way.
    label = f"{name} (free text)" if is_text else name
    bits: list[str] = []
    desc = d.get("desc")
    if isinstance(desc, str) and desc.strip():
        # Fragments are joined with "; " and end with a single ".", so remove
        # any trailing period the author put on the description.
        bits.append(desc.strip().rstrip("."))
    if is_text:
        # Free text has no range and no bands, so the marker is the whole
        # entry. It still earns a line without a description, because the
        # marker is what stops the model sending a delta.
        return f"{label} — {'; '.join(bits)}." if bits else f"{label}."
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
    return f"{label} — {'; '.join(bits)}." if bits else None


def render_reference(stat_schema: dict, player_name: str = "") -> str:
    """Returns a fixed, per-scenario legend for the stats.

    Each line gives what a stat means, from its `desc`, and its band ladder. The
    legend does not change from turn to turn, and it is separate from the live
    values.

    `player_name` names the persona, so that the guide ties the name the model
    reads in the story to the `player.` paths it has to write. The persona's
    description is deliberately not repeated here: it has its own section, and
    this guide is about paths.
    """
    lines: list[str] = []
    if player_name:
        lines.append(
            f"Protagonist {player_name} — the player character; their stats are "
            f"addressed as player.<stat>."
        )
    for section in STAT_SECTIONS:
        for name, d in (stat_schema.get(section) or {}).items():
            if isinstance(d, dict):
                row = _describe_stat(f"{section}.{name}", d)
                if row:
                    lines.append(row)
    for npc_key, ndef in (stat_schema.get("npcs") or {}).items():
        if not isinstance(ndef, dict):
            continue
        name = npc_name(ndef, npc_key)
        desc = ndef.get("desc")
        # The header is written even for an NPC with no description, because it
        # is the only line that ties a display name to the id the AI has to
        # address. The live values state it too, but only for the NPCs a scene
        # has mentioned, so without this an NPC off screen can only be guessed
        # at — and a guess is refused as a character or a stat that does not
        # exist.
        head = f"NPC {name} (npc.{npc_key})"
        lines.append(
            f"{head} — {desc.strip().rstrip('.')}."
            if isinstance(desc, str) and desc.strip() else f"{head}."
        )
        for sname, sdef in (ndef.get("stats") or {}).items():
            if isinstance(sdef, dict):
                row = _describe_stat(f"npc.{npc_key}.{sname}", sdef)
                if row:
                    lines.append(row)
    for name, d in (stat_schema.get("flags") or {}).items():
        if isinstance(d, dict):
            desc = d.get("desc")
            if isinstance(desc, str) and desc.strip():
                lines.append(f"flags.{name} — {desc.strip().rstrip('.')}.")
    if not lines:
        return ""
    return "Stat guide (fixed reference):\n" + "\n".join(f"- {ln}" for ln in lines)
