"""The adventure endpoints, split across one module per group of routes.

Importing this package registers every route, because each endpoint module
decorates its handlers with the shared `router` from `deps`. The import order
below is the order FastAPI matches paths in. No two routes here shadow each
other, so the order is for reading rather than for correctness.

Read the modules in this order to follow a turn from end to end:

    deps           the router and the ownership check every endpoint runs
    paging         reading a window of actions and numbering its attempts
    nodes          moving around the story tree
    turns          playing a turn, and the lock that allows only one at a time
    takes          retries and the attempts that collect at one coordinate
    branches       where a story splits

What this package re-exports, and what it deliberately does not:

Pure helpers and handlers are re-exported below, so `adventures.ACTION_PAGE` and
`adventures.undo_turn` keep working. The names a test replaces are not, and you
must reach those as `adventures.turns.<name>`. Rebinding a re-exported alias
changes only the alias, so patching `adventures.generate_turn` would leave every
caller reading the original. Leaving those names off raises `AttributeError`
instead, which is the failure you want.
"""
from .deps import router

# Imported for the side effect of registering routes. The names are unused here.
from . import (  # noqa: F401
    crud,
    turns,
    takes,
    branches,
    bundle_io,
    scripts,
    refresh,
    insights,
    memories,
    actions,
)
from ... import limits  # noqa: F401  `adventures.limits` is patched by tests.
from .crud import SNIPPET_MAX, _snippet
from .paging import ACTION_PAGE
from .takes import retry_action, undo_turn
from .turns import world_delta_of

__all__ = [
    "ACTION_PAGE",
    "SNIPPET_MAX",
    "_snippet",
    "limits",
    "retry_action",
    "router",
    "undo_turn",
    "world_delta_of",
]
