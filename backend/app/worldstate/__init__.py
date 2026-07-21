"""Phase 12 — RPG world state: the AI proposes stat/milestone deltas, this
module validates and clamps them against a scenario's stat_schema."""

from .engine import (
    EMIT_RULE,
    apply_delta,
    apply_override,
    band_label,
    extract_delta,
    has_schema,
    instantiate,
    npc_name,
    npc_triggers,
    render_reference,
    render_state_section,
)

__all__ = [
    "EMIT_RULE",
    "apply_delta",
    "apply_override",
    "band_label",
    "extract_delta",
    "has_schema",
    "instantiate",
    "npc_name",
    "npc_triggers",
    "render_reference",
    "render_state_section",
]
