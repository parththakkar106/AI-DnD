"""What a v2 bundle costs, measured on a production-sized adventure.

SP6 gave the bundle coordinates, and coordinates cost bytes: a branch number
and a depth on every node, plus the two after-snapshots a switch needs to put
back. This puts a number on that, against the v1 shape it replaces, on the same
fixture the egress work is measured on.

    python -m tools.measure_bundle --actions 600 --rich
    python -m tools.measure_bundle --actions 600 --rich --forks 20

Reuses `tools.stress_session`'s fixture, so read that module's warnings first:
a `--rich` run is a correctness fixture, and its byte figures are not
comparable to a plain one.
"""
import json
import random
import sys

# Import `stress_session` first. This is not a style preference. Importing it is
# what points `AIDND_DB_PATH` at a throwaway file, and `app.database` reads that
# at module scope, so an `app` import above this line runs the whole fixture
# against `backend/data.db` instead. The failure looks like success: the first
# run seeds a synthetic user and adventure into the local database and reports
# plausible numbers, and only the second run fails on the unique email.
from tools import stress_session as stress  # noqa: I001  (see above)

from app import bundle, models, tree
from app.database import SessionLocal


def _v1_shape(v2: dict) -> dict:
    """The same story as v1 would have written it, for a like-for-like count.

    There is one entry per turn, with the siblings collected back into a
    `variants` array, no coordinates, and no outcomes. That is what version 1
    could carry.
    """
    turns: dict[tuple[int, int], list[dict]] = {}
    order: list[tuple[int, int]] = []
    for node in v2["actions"]:
        key = (node["branch"], node["depth"])
        if key not in turns:
            turns[key] = []
            order.append(key)
        turns[key].append(node)
    actions = []
    for i, key in enumerate(order):
        group = turns[key]
        live = next((n for n in group if n["live"]), group[0])
        actions.append({
            "index": i, "type": live["type"], "text": live["text"],
            "reasoning": live.get("reasoning"),
            "variants": [
                {"text": n["text"], "reasoning": n.get("reasoning"),
                 "createdAt": n["createdAt"]}
                for n in group
            ] if len(group) > 1 else None,
            "variantIndex": group.index(live),
            "createdAt": live["createdAt"],
        })
    old = {k: v for k, v in v2.items() if k not in ("branches", "headBranch")}
    old["format"] = bundle.LEGACY_FORMAT
    old["actions"] = actions
    old["memories"] = [
        {k: v for k, v in m.items() if k not in ("branch", "depth")}
        for m in v2["memories"]
    ]
    old["memoryCursor"] = 0
    old["summaryCursor"] = 0
    return old


def _bytes(obj) -> int:
    return len(json.dumps(obj).encode("utf-8"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    forks = 0
    if "--forks" in argv:
        i = argv.index("--forks")
        forks = int(argv[i + 1])
        del argv[i:i + 2]
    args = stress.parse_args(argv)
    rng = random.Random(args.seed)
    stress._build_text(args, random.Random(args.seed ^ 0x5F5F))
    adv_id, _ = stress.build_fixture(args, rng)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, adv_id)
        if forks:
            # Twenty divergences off one line, each a little deeper, which is
            # the shape SP5 measured the fork cost on. The nodes are chosen up
            # front and the session is flushed after every fork. `fork` moves a
            # row onto its new branch, and with `autoflush=False` a query issued
            # before that move is written still finds the node at its old
            # location.
            root = adventure.head_branch_id
            candidates = [
                node.id for node in
                db.query(models.Action)
                .filter(
                    models.Action.adventure_id == adventure.id,
                    models.Action.branch_id == root,
                    models.Action.depth > 1,
                )
                .order_by(models.Action.depth)
                .all()
            ]
            step = max(len(candidates) // (forks + 1), 1)
            made = 0
            for node_id in candidates[::step][:forks]:
                tree.fork(db, adventure, db.get(models.Action, node_id))
                db.flush()
                made += 1
            db.commit()
            print(f"forked {made} times")

        v2 = bundle.export(db, adventure)
        v1 = _v1_shape(v2)
        nodes = len(v2["actions"])
        turns = len({(n["branch"], n["depth"]) for n in v2["actions"]})
        branches = len(v2["branches"])

        stripped = json.loads(json.dumps(v2))
        for node in stripped["actions"]:
            node.pop("stateAfter", None)
            node.pop("worldStateAfter", None)
            node.pop("worldDelta", None)

        v2_bytes, v1_bytes, bare = _bytes(v2), _bytes(v1), _bytes(stripped)
        print(f"{turns} turns · {nodes} nodes · {branches} branches")
        print(f"v1 shape        {v1_bytes:>12,} B")
        print(f"v2              {v2_bytes:>12,} B   "
              f"{v2_bytes / v1_bytes:.3f}× v1")
        print(f"v2 w/o outcomes {bare:>12,} B   "
              f"{bare / v1_bytes:.3f}× v1")
        print(f"the outcomes    {v2_bytes - bare:>12,} B   "
              f"{(v2_bytes - bare) / nodes:.1f} B/node")
        print(f"coordinates     {bare - v1_bytes:>12,} B   "
              f"{(bare - v1_bytes) / nodes:+.1f} B/node")
        print(f"import cap      {bundle.__name__}: "
              f"{v2_bytes / (20 * 1024 * 1024):.1%} of MAX_IMPORT_BODY_BYTES")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
