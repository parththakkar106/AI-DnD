"""Phase 12: RPG world state.

The scenario carries a `stat_schema`, which is the template: which stats exist,
their bands and rules, and the milestones. An adventure carries a live
`world_state` instantiated from it. Each turn the AI proposes a delta holding
only what changed. `apply_delta` decides what the delta is allowed to do. It
clamps values to min and max, caps the change per turn, enforces cooldowns, and
makes milestones sticky.

Nothing here raises on bad AI output. A malformed delta returns `{}` and the turn
continues, the same way a broken script never breaks a turn.

The work is split four ways, and each module reads on its own:

    schema     what a scenario's `stat_schema` allows, and building state from it
    parse      the block the model writes, and reading it back
    apply      every write to world state, and the limits it is held to
    render     turning state and schema into prompt text

Import the names from this package rather than from those modules. The split is
an implementation detail, and the names below are the interface.
"""

from .apply import apply_delta, apply_override
from .parse import (
    EMIT_REMINDER,
    EMIT_RULE,
    applied_delta,
    extract_delta,
    refusals,
    render_delta_block,
    render_refusals,
)
from .render import render_reference, render_state_section
from .schema import band_label, has_schema, instantiate, npc_name, npc_triggers, reconcile

__all__ = [
    "EMIT_REMINDER",
    "EMIT_RULE",
    "applied_delta",
    "apply_delta",
    "apply_override",
    "band_label",
    "extract_delta",
    "has_schema",
    "instantiate",
    "npc_name",
    "npc_triggers",
    "reconcile",
    "refusals",
    "render_delta_block",
    "render_reference",
    "render_refusals",
    "render_state_section",
]
