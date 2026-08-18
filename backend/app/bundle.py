"""Phase 14, SP6 — the export bundle, as a tree.

A bundle is the one place a story leaves the database, and the only part of the
tree no migration can ever reach: a file downloaded today has to still import
into a build shipped next year. So the format is versioned, both versions live
here, and nothing else in the app knows either of them.

**v1** is a flat list of turns, each with an optional `variants` array — the
repeating group SP4 unpacked into rows. Nothing writes that shape any more.
The *reader* stays, because bundles already on people's disks still have it and
a backup that stops importing is not a backup.

**v2** carries the tree. Three things it holds that v1 could not, each
load-bearing:

* **the branches**, because a forked adventure is two stories and a flat list
  can hold one — v1 export interleaved them by `index`, which read as a mangled
  story rather than as lost data;
* **`live`**, because a coordinate can hold several attempts at one turn and
  exactly one of them is the story;
* **both after-snapshots**, because they are what a branch switch and an undo
  put back. A bundle carrying the actions but not the outcomes would import a
  tree nobody could switch inside.

## The rule about what a bundle carries

**What was chosen, never what is derived.** The head *branch*, the fork points,
the live flags and the anchors are decisions somebody made; they are in the
file. `lineage`, the head *depth*, `index` and the variant ordinals are all
computed from those, and they are recomputed on import instead:

* `lineage` is a cache of `parent` + `fork_depth`. Shipping it too would put a
  second source of truth for one fact in a file anybody can hand-edit, and the
  two could then disagree in a way no read would ever report.
* the head depth is the tip of the head branch, which is a fact about the nodes
  that arrived with it.
* `index` is the legacy column SP8 drops. Its one remaining job is to hand the
  next row a number nothing else holds — a fact about the *adventure*, not
  about a path — so `depth` cannot be it: two branches have a node at depth 4.
  The import allocates one per turn instead, which keeps `max_action_index`
  honest and keeps siblings sharing an index the way SP4 leaves them.
* the variant ordinals are `attempts.renumber`'s to maintain, and it is the
  only place allowed to.

Every hand-editable coordinate is therefore checked before a row is written
(`plan`), not fixed up afterwards: an import that fails halfway leaves an
adventure holding half a tree, and a tree missing a branch is a story that
silently stops rather than one that reports.
"""

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import insert, update
from sqlalchemy.orm import Session, undefer

from . import attempts, models, schemas
from .context import cursors, lineage

FORMAT = "ai-dnd-adventure-v2"
LEGACY_FORMAT = "ai-dnd-adventure-v1"

# VARCHAR(20) on `actions.type`; a raw-dict import bypasses the schemas.
TYPE_MAX = 20


# ---------------------------------------------------------------- exporting

def export(db: Session, adventure: models.Adventure) -> dict:
    """The whole adventure as a v2 bundle.

    Deliberately un-pathed: a backup wants the entire tree, not the branch its
    owner happens to be standing on. Both after-snapshots are undeferred in the
    one query — they are per-node columns nothing else reads in bulk, and asking
    for them a row at a time would be a query per turn.

    No context snapshots. A bundle has never carried the assembled prompts and
    still does not: they are ~163 kB a turn, they are an explanation of a
    generation rather than part of the story, and the Insights viewer they feed
    is reading the adventure it came from.
    """
    branches = (
        db.query(models.Branch)
        .filter(models.Branch.adventure_id == adventure.id)
        .order_by(models.Branch.id)
        .all()
    )
    # Branch ids are local to the file — positions in this list — because the
    # database ids they had here are already taken over there.
    local = {branch.id: i for i, branch in enumerate(branches)}
    nodes = (
        db.query(models.Action)
        .filter(models.Action.adventure_id == adventure.id)
        .options(
            undefer(models.Action.state_after),
            undefer(models.Action.world_state_after),
        )
        .order_by(
            models.Action.branch_id, models.Action.depth,
            models.Action.variant_index, models.Action.id,
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
        "scriptState": adventure.script_state,
        "worldState": adventure.world_state,
        "autoSummarize": adventure.auto_summarize,
        "memoryBankEnabled": adventure.memory_bank_enabled,
        # A root entry even for an adventure whose branch row was never created
        # — a story with no branch is a pre-tree one, and the tree it belongs to
        # is the root. `_local` puts its nodes there.
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
    # A name is something a player chose, so it travels — the same rule that
    # puts the fork points in the file and leaves `lineage` out. An unnamed
    # branch omits the key rather than carrying a null, which keeps the file
    # for a tree nobody has named byte-identical to the one SP6 wrote.
    if branch.name:
        out["name"] = branch.name
    return out


def _exported_node(action: models.Action, local: dict[int, int]) -> dict:
    node = {
        "branch": _local(action.branch_id, local),
        # A pre-tree row's depth is the number `index` already held.
        "depth": action.depth if action.depth is not None else action.index,
        "live": bool(action.live),
        "type": action.type,
        "text": action.text,
        "createdAt": action.created_at.isoformat() if action.created_at else None,
    }
    if action.reasoning:
        node["reasoning"] = action.reasoning
    # `{}` and absent mean different things — "this node left an empty
    # scoreboard behind" against "nobody knows, leave the live state alone" —
    # so an empty snapshot is written out rather than trimmed. It costs about
    # eighteen bytes a row and it is the difference between an undo that clears
    # a score and one that leaves it standing.
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
        # The node it hangs off. A hand-written memory summarises no node, so it
        # has a branch and no depth, and keeps that shape here.
        "branch": _local(memory.branch_id, local),
        "depth": memory.depth,
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
    """The bundle's version, or 400."""
    fmt = bundle.get("format")
    if fmt in (FORMAT, LEGACY_FORMAT):
        return fmt
    raise HTTPException(
        400,
        f"Not an adventure export file (expected format {FORMAT} or {LEGACY_FORMAT}).",
    )


def plan(bundle: dict, version: str) -> dict:
    """The bundle's tree, checked and normalised, before a row is written.

    Pure: no session, no adventure, nothing created. Everything a hand-edited
    file can get wrong about the *shape* of a tree is caught here, because the
    alternative is an import that fails partway and leaves an adventure holding
    a story with a hole in it.

    Both versions land in the same shape, so `write` never learns there are two
    formats: a v1 bundle is a linear story, which is a tree with one branch, and
    its `variants` array is a sibling group written the old way.
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
        # v2 knows where the derived work got to; v1 counted it, and a count
        # cannot be turned into a node until the nodes exist (see `settle`).
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
        # A branch may only fork from one listed before it. That is how the
        # export writes them — branches are numbered in creation order and a
        # parent always exists first — and requiring it here buys acyclicity for
        # the price of a comparison: a lineage is computed by walking to the
        # parent, and a cycle would be an import that never returns.
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
    """The name a branch entry carries, or None for one nobody named.

    Checked before the row is created rather than left to the column, for the
    reason the whole planner exists: a 400 from a pure function beats a half
    written adventure and a database error from three branches in.
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
    """A v1 bundle's turns as the nodes they describe: one per attempt.

    The `variants` array is the repeating group SP4 unpacked, so reading one is
    the same split migration 60 does — every attempt becomes a row at the turn's
    coordinate and `variantIndex` picks which is live. Clamped, because a
    hand-edited bundle can name an attempt its own list does not have, and a
    turn with no live node is a turn no read can see.

    The depth is the bundle's `index`: v1 is one branch, where the two agree.
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
            # Out of range rather than absent means a file that disagrees with
            # itself; the root is the safe reading, because a memory on a branch
            # nothing can see is a memory that never reaches a prompt again.
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
    """Write a planned tree onto a freshly created adventure.

    Order matters and is not negotiable: branches first, because a node needs an
    id to hang off; then the nodes, because the head and the anchors name one.
    """
    ids = _write_branches(db, adventure, story["branches"])
    _write_nodes(db, adventure, story["nodes"], ids)
    _write_memories(db, adventure, story["memories"], ids)
    _point_the_head(adventure, story, ids)
    _write_anchors(adventure, story, ids)


def _write_branches(
    db: Session, adventure: models.Adventure, specs: list[dict]
) -> list[int]:
    """One row per branch, lineage computed rather than read.

    Inserted through Core and the lineage written second, for the reason
    `tree.root_branch` spells out: the lineage names the row's own id. The
    parent's cached ancestry is capped at this fork, which is the same
    arithmetic `tree.fork` does — a fork made now and a fork made a year ago and
    exported must produce the same rows.
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
    """The nodes, grouped into the turns they are attempts at.

    Two things are allocated here rather than trusted from the file. `index` is
    handed out one per *turn*, in the order the bundle lists them, so siblings
    share one and no two coordinates do — which is what `max_action_index` needs
    to keep issuing numbers nothing holds. And exactly one attempt in each group
    is made live: a file can name none or several, and a turn with no live node
    is a turn that vanishes from the story.
    """
    groups: dict[tuple[int, int], list[models.Action]] = {}
    indices: dict[tuple[int, int], int] = {}
    for spec in specs:
        key = (spec["branch"], spec["depth"])
        if key not in indices:
            indices[key] = len(indices)
        action = models.Action(
            adventure_id=adventure.id,
            branch_id=ids[spec["branch"]],
            depth=spec["depth"],
            index=indices[key],
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
        attempts.renumber(rows)


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
        # A v1 memory has no depth of its own; `source_end` is the index of the
        # last action it summarises, which on one branch is that node's depth.
        if memory.depth is None and memory.source_end is not None:
            memory.depth = memory.source_end
        db.add(memory)


def _point_the_head(
    adventure: models.Adventure, story: dict, ids: list[int]
) -> None:
    """Where the story is being played, and how deep it goes.

    The branch comes from the file and the depth does not: the tip of a branch
    is whatever arrived on it, and a branch with nothing of its own sits at its
    fork point — the last node its story contains, borrowed but the tip all the
    same. The same rule as `tree.refresh_head`, applied before a flush.
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
    """How far the memories and the summary have read — v2 only.

    A v1 bundle counts instead, and a count cannot be resolved to a node until
    the nodes are in the database; `settle` does that half afterwards.
    """
    anchors = story["anchors"]
    if anchors is None:
        return
    for cursor in cursors.ALL:
        branch, depth = anchors[cursor.name]
        cursor.anchor(adventure, ids[branch] if branch is not None else None, depth)


def settle(db: Session, adventure: models.Adventure, story: dict) -> None:
    """Line up the two coordinate systems, once the nodes exist.

    The anchors and the legacy counts describe the same boundary in different
    words, and each version of the bundle brings one of them. A v1 file brings
    the count, so the anchor is found by counting that far along the story; a v2
    file brings the anchor, so the count is read back off it. The legacy columns
    are still written either way — they are what a rolled-back build reads.

    Called after the flush, because both directions need the actions queryable.
    """
    positions = story["positions"]
    for cursor in cursors.ALL:
        if positions is not None:
            setattr(adventure, f"{cursor.name}_cursor", positions[cursor.name])
            cursors.anchor_at_position(adventure, cursor, positions[cursor.name])
        else:
            setattr(
                adventure, f"{cursor.name}_cursor",
                cursors.position_of(adventure, cursor.depth(db, adventure)),
            )


# ------------------------------------------------------------------ reading
# Small coercions. A raw-dict import bypasses the schemas entirely, so
# everything out of a bundle is whatever JSON happened to hold.

def _is_int(value) -> bool:
    """`True` is an `int` in Python, and is not one in a coordinate."""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(value, default: int) -> int:
    return value if _is_int(value) else default


def _as_index(value, count: int, default: int | None = None) -> int | None:
    """A local branch number, or `default` when the file names one that is not
    there. Out of range is a file disagreeing with itself, not a shape a read
    can be handed."""
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
