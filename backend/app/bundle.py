"""Phase 14, SP6: the export bundle, as a tree.

A bundle is the one place a story leaves the database, and the only part of the
tree no migration can reach. A file downloaded today has to still import into a
build shipped next year. The format is therefore versioned, both versions are
defined here, and nothing else in the app knows either of them.

Version 1 is a flat list of turns, each with an optional `variants` array, which
is the repeating group SP4 unpacked into rows. Nothing writes that shape now.
The reader stays, because bundles already on people's disks still use it, and a
backup that stops importing is not a backup.

Version 2 carries the tree. It holds three things version 1 could not, and each
one is required:

* The branches, because a forked adventure is two stories and a flat list holds
  one. A version 1 export interleaved them by turn number, which read as a
  garbled story rather than as lost data.
* `live`, because a coordinate can hold several attempts at one turn and exactly
  one of them is the story.
* Both after-snapshots, because they are what a branch switch and an undo
  restore. A bundle carrying the actions but not the outcomes would import a
  tree nobody could switch inside.

## The rule about what a bundle carries

A bundle carries what was chosen, never what is derived. The head branch, the
fork points, the live flags, and the anchors are decisions somebody made, so
they are in the file. `lineage` and the head depth are computed from those, and
the import recomputes them:

* `lineage` is a cache of `parent` plus `fork_depth`. Shipping it as well would
  put a second source of truth for one fact into a file anyone can hand-edit,
  and the two could then disagree without any read reporting it.
* The head depth is the tip of the head branch, which is a fact about the nodes
  that arrived with it.

Every hand-editable coordinate is therefore checked before a row is written, in
`plan`, rather than repaired afterwards. An import that fails partway leaves an
adventure holding half a tree, and a tree missing a branch is a story that stops
without reporting anything.
"""

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import insert, update
from sqlalchemy.orm import Session, undefer

from . import attempts, models, schemas
from .context import cursors, lineage

FORMAT = "ai-dnd-adventure-v2"
LEGACY_FORMAT = "ai-dnd-adventure-v1"

# `actions.type` is VARCHAR(20), and a raw-dict import bypasses the schemas.
TYPE_MAX = 20


# ---------------------------------------------------------------- exporting

def export(db: Session, adventure: models.Adventure) -> dict:
    """Returns the whole adventure as a version 2 bundle.

    The export is not scoped to a path. A backup holds the entire tree, not the
    branch its owner is currently reading. Both after-snapshots are undeferred in
    the one query, because they are per-node columns nothing else reads in bulk
    and requesting them a row at a time would cost one query per turn.

    The bundle carries no context snapshots. It has never carried the assembled
    prompts, and it still does not. They run about 163 kB per turn, they explain
    a generation rather than form part of the story, and the Insights viewer they
    feed reads the adventure they came from.
    """
    branches = (
        db.query(models.Branch)
        .filter(models.Branch.adventure_id == adventure.id)
        .order_by(models.Branch.id)
        .all()
    )
    # Branch ids are local to the file and are positions in this list, because
    # the database ids they hold here are already in use on the importing
    # side.
    local = {branch.id: i for i, branch in enumerate(branches)}
    nodes = (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adventure.id)
        .options(
            undefer(models.Action.state_after),
            undefer(models.Action.world_state_after),
        )
        .order_by(
            models.Action.branch_id, models.Action.depth, models.Action.id,
        )
        .all()
    )
    return {
        "format": FORMAT,
        "title": adventure.title,
        "memory": adventure.memory,
        "authorsNote": adventure.authors_note,
        "aiInstructions": adventure.ai_instructions,
        "storySummary": adventure.story_summary,
        # Phase 18. A bundle written before personas existed has no key here,
        # and the import below reads it with `.get`, so it lands with an empty
        # persona — which is the same as having none. No FORMAT bump needed.
        "persona": {
            "name": adventure.persona_name,
            "pronouns": adventure.persona_pronouns,
            "desc": adventure.persona_desc,
        },
        "scriptState": adventure.script_state,
        "worldState": adventure.world_state,
        "autoSummarize": adventure.auto_summarize,
        "memoryBankEnabled": adventure.memory_bank_enabled,
        # Write a root entry even for an adventure whose branch row was never
        # created. A story with no branch is a pre-tree story, and the tree it
        # belongs to is the root. `_local` places its nodes there.
        "branches": [_exported_branch(b, local) for b in branches] or [_ROOT],
        "headBranch": local.get(adventure.head_branch_id, 0),
        "memoryCursor": _exported_anchor(adventure, cursors.MEMORY, local),
        "summaryCursor": _exported_anchor(adventure, cursors.SUMMARY, local),
        "memories": [_exported_memory(m, local) for m in adventure.memories],
        "storyCards": [
            {"type": c.type, "name": c.name, "keys": c.keys,
             "entry": c.entry, "notes": c.notes}
            for c in adventure.story_cards
        ],
        "scripts": [
            {
                "position": s.position, "enabled": s.enabled,
                "name": s.name, "description": s.description,
                "library": s.library_js, "input": s.input_js,
                "context": s.context_js, "output": s.output_js,
            }
            for s in adventure.scripts
        ],
        "actions": [_exported_node(a, local) for a in nodes],
    }


_ROOT = {"parent": None, "forkDepth": None}


def _local(branch_id: int | None, local: dict[int, int]) -> int:
    return local.get(branch_id, 0) if branch_id is not None else 0


def _exported_branch(branch: models.Branch, local: dict[int, int]) -> dict:
    parent = (
        local.get(branch.parent_branch_id)
        if branch.parent_branch_id is not None else None
    )
    out = (
        dict(_ROOT) if parent is None
        else {"parent": parent, "forkDepth": branch.fork_depth}
    )
    # A player chose the name, so it goes into the file. That is the same rule
    # that puts the fork points in the file and leaves `lineage` out. An unnamed
    # branch omits the key rather than carry a null, which keeps the file for an
    # unnamed tree byte-identical to the one SP6 wrote.
    if branch.name:
        out["name"] = branch.name
    return out


def _exported_node(action: models.Action, local: dict[int, int]) -> dict:
    node = {
        "branch": _local(action.branch_id, local),
        "depth": action.depth,
        "live": bool(action.live),
        "type": action.type,
        "text": action.text,
        "createdAt": action.created_at.isoformat() if action.created_at else None,
    }
    if action.reasoning:
        node["reasoning"] = action.reasoning
    # `{}` and an absent key mean different things. `{}` means the node left an
    # empty state behind, and an absent key means the state is unknown and the
    # live state stays as it is. An empty snapshot is therefore written rather
    # than omitted. It costs about eighteen bytes per row, and it decides
    # whether an undo clears a score or leaves it in place.
    if action.state_after is not None:
        node["stateAfter"] = action.state_after
    if action.world_state_after is not None:
        node["worldStateAfter"] = action.world_state_after
    if action.world_delta:
        node["worldDelta"] = action.world_delta
    return node


def _exported_memory(memory: models.Memory, local: dict[int, int]) -> dict:
    return {
        "text": memory.text, "pinned": memory.pinned, "forgotten": memory.forgotten,
        "sourceStart": memory.source_start, "sourceEnd": memory.source_end,
        "useCount": memory.use_count,
        # The node this memory is attached to. A hand-written memory summarizes
        # no node, so it has a branch and no depth, and it keeps that shape
        # here.
        "branch": _local(memory.branch_id, local),
        "depth": memory.depth,
    }


def _imported_persona(persona) -> dict:
    """Reads a bundle's `persona` block into `Adventure` keyword arguments.

    A raw-dict import bypasses the schemas, so the strings are truncated to the
    widths the columns declare, in the same way the rest of `_import` does. A
    bundle written before Phase 18 has no block at all, and an empty persona is
    the same as having none.
    """
    if not isinstance(persona, dict):
        return {}
    return {
        "persona_name": str(persona.get("name") or "")[:schemas.PERSONA_NAME_MAX],
        "persona_pronouns":
            str(persona.get("pronouns") or "")[:schemas.PERSONA_PRONOUNS_MAX],
        "persona_desc": str(persona.get("desc") or ""),
    }


def _exported_anchor(
    adventure: models.Adventure, cursor: cursors.Cursor, local: dict[int, int]
) -> dict:
    branch_id, depth = cursor.stored(adventure)
    return {
        "branch": local.get(branch_id) if branch_id is not None else None,
        "depth": depth,
    }


# ---------------------------------------------------------------- importing

def check_format(bundle: dict) -> str:
    """Returns the bundle's version, or raises a 400."""
    fmt = bundle.get("format")
    if fmt in (FORMAT, LEGACY_FORMAT):
        return fmt
    raise HTTPException(
        400,
        f"Not an adventure export file (expected format {FORMAT} or {LEGACY_FORMAT}).",
    )


def plan(bundle: dict, version: str) -> dict:
    """Returns the bundle's tree, checked and normalized, before a row is written.

    The function has no side effects. It opens no session, needs no adventure,
    and creates nothing. It catches everything a hand-edited file can get wrong
    about the shape of a tree, because the alternative is an import that fails
    partway and leaves an adventure holding a story with a gap in it.

    Both versions produce the same shape, so `write` never learns that there are
    two formats. A version 1 bundle is a linear story, which is a tree with one
    branch, and its `variants` array is a sibling group written the old way.
    """
    branches = (
        _planned_branches(bundle) if version == FORMAT else [dict(_ROOT)]
    )
    nodes = (
        _planned_nodes(bundle, len(branches)) if version == FORMAT
        else _planned_v1_nodes(bundle)
    )
    return {
        "branches": branches,
        "nodes": nodes,
        "memories": _planned_memories(bundle, len(branches)),
        "head": _as_index(bundle.get("headBranch"), len(branches), default=0),
        # Version 2 records where the derived work reached. Version 1 counted
        # it, and a count cannot become a node until the nodes exist. See
        # `settle`.
        "anchors": _planned_anchors(bundle, len(branches)) if version == FORMAT else None,
        "positions": None if version == FORMAT else {
            "memory": _as_int(bundle.get("memoryCursor"), 0),
            "summary": _as_int(bundle.get("summaryCursor"), 0),
        },
    }


def _planned_branches(bundle: dict) -> list[dict]:
    raw = bundle.get("branches")
    entries = [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []
    if not entries:
        return [dict(_ROOT)]
    specs: list[dict] = []
    for i, entry in enumerate(entries):
        parent = entry.get("parent")
        name = _planned_branch_name(entry, i)
        if parent is None:
            specs.append(dict(_ROOT, **({"name": name} if name else {})))
            continue
        # A branch may fork only from a branch listed before it. The export
        # writes them that way, because branches are numbered in creation order
        # and a parent always exists first. Requiring it here guarantees the
        # graph is acyclic for the cost of one comparison. A lineage is computed
        # by walking to the parent, so a cycle would be an import that never
        # returns.
        if not _is_int(parent) or not 0 <= parent < i:
            raise HTTPException(
                400,
                f"Branch {i} forks from branch {parent!r}, which is not one of "
                f"the {i} branches listed before it.",
            )
        fork_depth = entry.get("forkDepth")
        if not _is_int(fork_depth):
            raise HTTPException(
                400,
                f"Branch {i} forks from branch {parent} but does not say at "
                f"what depth.",
            )
        specs.append({
            "parent": parent, "forkDepth": fork_depth,
            **({"name": name} if name else {}),
        })
    return specs


def _planned_branch_name(entry: dict, i: int) -> str | None:
    """Returns the name a branch entry carries, or `None` if nobody named it.

    The check runs before the row is created rather than being left to the
    column, for the reason the planner exists. A 400 from a function with no side
    effects is better than a half-written adventure and a database error three
    branches in.
    """
    raw = entry.get("name")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(400, f"Branch {i} has a name that is not text.")
    name = raw.strip()
    if len(name) > schemas.BRANCH_NAME_MAX:
        raise HTTPException(
            400,
            f"Branch {i}'s name is longer than {schemas.BRANCH_NAME_MAX} "
            f"characters.",
        )
    return name or None


def _planned_nodes(bundle: dict, branches: int) -> list[dict]:
    raw = bundle.get("actions")
    nodes: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not str(entry.get("text") or ""):
            continue
        branch = entry.get("branch", 0)
        if not _is_int(branch) or not 0 <= branch < branches:
            raise HTTPException(
                400,
                f"An action names branch {branch!r}, but the file lists "
                f"{branches}.",
            )
        depth = entry.get("depth")
        if not _is_int(depth) or depth < 0:
            raise HTTPException(
                400, f"An action on branch {branch} has no depth to sit at."
            )
        nodes.append({
            "branch": branch,
            "depth": depth,
            "live": bool(entry.get("live", True)),
            "type": str(entry.get("type") or "story")[:TYPE_MAX],
            "text": str(entry.get("text") or ""),
            "reasoning": _as_text(entry.get("reasoning")),
            "stateAfter": _as_dict(entry.get("stateAfter")),
            "worldStateAfter": _as_dict(entry.get("worldStateAfter")),
            "worldDelta": _as_dict(entry.get("worldDelta")),
            "createdAt": _as_time(entry.get("createdAt")),
        })
    return nodes


def _planned_v1_nodes(bundle: dict) -> list[dict]:
    """Returns a version 1 bundle's turns as nodes, one node per attempt.

    The `variants` array is the repeating group SP4 unpacked, so reading one
    performs the same split that migration 60 does. Every attempt becomes a row
    at the turn's coordinate, and `variantIndex` selects the live one. The index
    is clamped, because a hand-edited bundle can name an attempt its own list
    does not contain, and a turn with no live node is a turn no read can see.

    The depth is the bundle's `index`. A version 1 bundle has one branch, where
    the two numbers agree.
    """
    raw = bundle.get("actions")
    nodes: list[dict] = []
    for i, entry in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(entry, dict) or not str(entry.get("text") or ""):
            continue
        depth = _as_int(entry.get("index"), i)
        kind = str(entry.get("type") or "story")[:TYPE_MAX]
        variants = [v for v in (entry.get("variants") or []) if isinstance(v, dict)]
        if not variants:
            variants = [{"text": entry["text"], "reasoning": entry.get("reasoning")}]
        live = min(max(_as_int(entry.get("variantIndex"), 0), 0), len(variants) - 1)
        for n, variant in enumerate(variants):
            text = str(variant.get("text") or "")
            if not text:
                continue
            nodes.append({
                "branch": 0,
                "depth": max(depth, 0),
                "live": n == live,
                "type": kind,
                "text": text,
                "reasoning": _as_text(variant.get("reasoning")),
                "stateAfter": None,
                "worldStateAfter": None,
                "worldDelta": None,
                "createdAt": _as_time(variant.get("createdAt")),
            })
    return nodes


def _planned_memories(bundle: dict, branches: int) -> list[dict]:
    raw = bundle.get("memories")
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
            continue
        out.append({
            "text": str(entry["text"]),
            "pinned": bool(entry.get("pinned", False)),
            "forgotten": bool(entry.get("forgotten", False)),
            "sourceStart": entry.get("sourceStart"),
            "sourceEnd": entry.get("sourceEnd"),
            "useCount": _as_int(entry.get("useCount"), 0),
            # A value that is out of range, rather than absent, means the file
            # disagrees with itself. The root is the safe reading, because a
            # memory on a branch nothing can see never reaches a prompt
            # again.
            "branch": _as_index(entry.get("branch"), branches, default=0),
            "depth": entry.get("depth") if _is_int(entry.get("depth")) else None,
        })
    return out


def _planned_anchors(bundle: dict, branches: int) -> dict:
    anchors = {}
    for name in ("memory", "summary"):
        raw = bundle.get(f"{name}Cursor")
        raw = raw if isinstance(raw, dict) else {}
        branch = raw.get("branch")
        anchors[name] = (
            _as_index(branch, branches) if branch is not None else None,
            _as_int(raw.get("depth"), lineage.NO_DEPTH),
        )
    return anchors


# ------------------------------------------------------------------ writing

def write(db: Session, adventure: models.Adventure, story: dict) -> None:
    """Writes a planned tree onto a newly created adventure.

    The order is fixed. Branches come first, because a node needs a branch id.
    The nodes come next, because the head and the anchors name a node.
    """
    ids = _write_branches(db, adventure, story["branches"])
    _write_nodes(db, adventure, story["nodes"], ids)
    _write_memories(db, adventure, story["memories"], ids)
    _point_the_head(adventure, story, ids)
    _write_anchors(adventure, story, ids)


def _write_branches(
    db: Session, adventure: models.Adventure, specs: list[dict]
) -> list[int]:
    """Writes one row per branch, computing the lineage rather than reading it.

    The rows are inserted through Core and the lineage is written second, for the
    reason `tree.root_branch` gives: the lineage names the row's own id. The
    parent's cached ancestry is capped at this fork, which is the arithmetic
    `tree.fork` performs. A fork made now and a fork made a year ago and then
    exported have to produce the same rows.
    """
    ids: list[int] = []
    lineages: list[list[list]] = []
    for spec in specs:
        parent = spec.get("parent")
        fork_depth = spec.get("forkDepth")
        new_id = db.execute(
            insert(models.Branch).values(
                adventure_id=adventure.id,
                parent_branch_id=ids[parent] if parent is not None else None,
                fork_depth=fork_depth if parent is not None else None,
                lineage=[],
                name=spec.get("name"),
                created_at=models.utcnow(),
            )
        ).inserted_primary_key[0]
        entries = [[new_id, None]]
        if parent is not None:
            entries += [
                [branch_id, fork_depth if cap is None else min(cap, fork_depth)]
                for branch_id, cap in lineages[parent]
            ]
        db.execute(
            update(models.Branch)
            .where(models.Branch.id == new_id)
            .values(lineage=entries)
        )
        ids.append(new_id)
        lineages.append(entries)
    return ids


def _write_nodes(
    db: Session, adventure: models.Adventure, specs: list[dict], ids: list[int]
) -> None:
    """Writes the nodes, grouped into the turns they are attempts at.

    One value is decided here rather than read from the file. Exactly one
    attempt in each group is made live, because a file can name none or several,
    and a turn with no live node disappears from the story.
    """
    groups: dict[tuple[int, int], list[models.Action]] = {}
    for spec in specs:
        key = (spec["branch"], spec["depth"])
        action = models.Action(
            adventure_id=adventure.id,
            branch_id=ids[spec["branch"]],
            depth=spec["depth"],
            type=spec["type"],
            text=spec["text"],
            reasoning=spec["reasoning"],
            live=spec["live"],
            state_after=spec["stateAfter"],
            world_state_after=spec["worldStateAfter"],
            world_delta=spec["worldDelta"],
        )
        if spec["createdAt"] is not None:
            action.created_at = spec["createdAt"]
        db.add(action)
        groups.setdefault(key, []).append(action)

    for rows in groups.values():
        live = next((row for row in rows if row.live), rows[0])
        for row in rows:
            row.live = row is live


def _write_memories(
    db: Session, adventure: models.Adventure, specs: list[dict], ids: list[int]
) -> None:
    for spec in specs:
        memory = models.Memory(
            adventure_id=adventure.id,
            text=spec["text"],
            pinned=spec["pinned"],
            forgotten=spec["forgotten"],
            source_start=spec["sourceStart"],
            source_end=spec["sourceEnd"],
            use_count=spec["useCount"],
            branch_id=ids[spec["branch"]],
            depth=spec["depth"],
        )
        # A version 1 memory has no depth of its own. `source_end` is the index
        # of the last action it summarizes, which on one branch is that node's
        # depth.
        if memory.depth is None and memory.source_end is not None:
            memory.depth = memory.source_end
        # A version 1 memory that summarizes nothing, which means one the player
        # typed, has no depth to derive. Leaving it NULL here would recreate on
        # import the state migration 62 exists to end. `Path._entry_clause`
        # compares `depth <= max_depth`, which a NULL fails, so the memory would
        # disappear from every branch as soon as the imported adventure was
        # forked. The root is the answer the migration gives, for the same
        # reason: 0 is at or before every fork point, so the memory is visible
        # from every path this adventure can grow.
        if memory.depth is None:
            memory.depth = lineage.ROOT_DEPTH
        db.add(memory)


def _point_the_head(
    adventure: models.Adventure, story: dict, ids: list[int]
) -> None:
    """Sets which branch the story is played on, and how deep it goes.

    The branch comes from the file and the depth does not. The tip of a branch is
    whatever arrived on it, and a branch with no nodes of its own sits at its
    fork point, which is the last node its story contains. That node is borrowed
    but it is still the tip. This is the rule `tree.refresh_head` applies, run
    here before a flush.
    """
    head = story["head"]
    adventure.head_branch_id = ids[head]
    depths = [n["depth"] for n in story["nodes"] if n["branch"] == head]
    if depths:
        adventure.head_depth = max(depths)
        return
    fork_depth = story["branches"][head].get("forkDepth")
    adventure.head_depth = fork_depth if fork_depth is not None else lineage.NO_DEPTH


def _write_anchors(
    adventure: models.Adventure, story: dict, ids: list[int]
) -> None:
    """Records how far the memories and the summary have read. Version 2 only.

    A version 1 bundle stores a count instead, and a count cannot be resolved to
    a node until the nodes are in the database. `settle` does that part
    afterwards.
    """
    anchors = story["anchors"]
    if anchors is None:
        return
    for cursor in cursors.ALL:
        branch, depth = anchors[cursor.name]
        cursor.anchor(adventure, ids[branch] if branch is not None else None, depth)


def settle(adventure: models.Adventure, story: dict) -> None:
    """Resolves a version 1 bundle's counts into anchors, once the nodes exist.

    A version 1 file records how far the memories and the summary have read as
    a count, so the anchor is found by counting that far along the story. A
    version 2 file carries the anchor itself, which `_write_anchors` has
    already stored, so there is nothing left to do.

    The caller runs this after the flush, because counting needs the actions to
    be queryable.
    """
    positions = story["positions"]
    if positions is None:
        return
    for cursor in cursors.ALL:
        cursors.anchor_at_position(adventure, cursor, positions[cursor.name])


def materialize(
    db: Session, payload: dict, story: dict, user_id: int | None
) -> models.Adventure:
    """Writes a planned bundle into a new adventure owned by `user_id`.

    Call `check_format` and `plan` first, and apply any rate or size limits
    before calling this. The split exists because those checks belong to the
    import endpoint alone: the guest starter writes a file the server ships, so
    it has nothing to rate-limit and no untrusted list to cap.

    The caller commits. Two flushes happen here, because the anchors and the
    legacy counts describe one boundary in two coordinate systems, and aligning
    them needs the actions to be queryable.
    """
    # A raw-dict import bypasses the schemas, so truncate strings bound for
    # VARCHAR columns. Postgres enforces the widths. See `schemas.py`.
    adventure = models.Adventure(
        user_id=user_id,
        title=str(payload.get("title") or "Imported Adventure")[:schemas.NAME_MAX],
        memory=str(payload.get("memory") or ""),
        authors_note=str(payload.get("authorsNote") or ""),
        ai_instructions=str(payload.get("aiInstructions") or ""),
        story_summary=str(payload.get("storySummary") or ""),
        script_state=payload.get("scriptState") or {},
        world_state=payload.get("worldState") or {},
        auto_summarize=bool(payload.get("autoSummarize", False)),
        memory_bank_enabled=bool(payload.get("memoryBankEnabled", False)),
        **_imported_persona(payload.get("persona")),
    )
    db.add(adventure)
    db.flush()

    for card in payload.get("storyCards") or []:
        if isinstance(card, dict):
            db.add(models.StoryCard(
                adventure_id=adventure.id,
                type=str(card.get("type") or "")[:schemas.CARD_TYPE_MAX],
                name=str(card.get("name") or "")[:schemas.NAME_MAX],
                keys=str(card.get("keys") or ""),
                entry=str(card.get("entry") or ""),
                notes=str(card.get("notes") or ""),
            ))

    for i, item in enumerate(payload.get("scripts") or []):
        if isinstance(item, dict):
            db.add(models.AdventureScript(
                adventure_id=adventure.id,
                position=int(item.get("position", i)),
                enabled=bool(item.get("enabled", True)),
                name=str(item.get("name") or "Imported Script")[:schemas.NAME_MAX],
                description=str(item.get("description") or ""),
                library_js=str(item.get("library") or ""),
                input_js=str(item.get("input") or ""),
                context_js=str(item.get("context") or ""),
                output_js=str(item.get("output") or ""),
            ))

    write(db, adventure, story)
    db.flush()
    db.expire(adventure, ["actions"])
    settle(adventure, story)
    return adventure

# ------------------------------------------------------------------ reading
# Small coercions. A raw-dict import bypasses the schemas, so every value from a
# bundle is whatever the JSON held.

def _is_int(value) -> bool:
    """Returns whether `value` is an integer. Python counts `True` as an `int`,
    and a coordinate does not."""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(value, default: int) -> int:
    return value if _is_int(value) else default


def _as_index(value, count: int, default: int | None = None) -> int | None:
    """Returns a local branch number, or `default` when the file names a branch
    that is not present.

    An out-of-range value means the file disagrees with itself, and it is not a
    value any read can be given.
    """
    return value if _is_int(value) and 0 <= value < count else default


def _as_text(value) -> str | None:
    return str(value) if value else None


def _as_dict(value) -> dict | None:
    return value if isinstance(value, dict) else None


def _as_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
