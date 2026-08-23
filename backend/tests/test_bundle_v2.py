"""Phase 14 SP6 — the export bundle carries the tree.

A bundle is the only part of this phase a migration can never reach: the file
is already on somebody's disk. So there are two formats, and the two halves of
this file watch different things.

**v2 has to be lossless for a story that forked**, which v1 could not be — it
had one list and there were two stories, so it interleaved them by `index` and
read as a mangled story. Losslessness here means the *tree*: every branch, the
fork point it left its parent at, which attempt at each turn is the story, and
what each node left behind — because that last one is what a branch switch puts
back, and a tree nobody can switch inside is not the tree that was exported.

**v1 has to still import**, because a backup that stops importing is not a
backup.

Underneath both is the rule the module is built on: a bundle carries what was
*chosen* and never what is *derived*. The lineage, the head depth, the legacy
`index` and the variant ordinals are all rebuilt on the way in, so a
hand-edited file cannot disagree with itself — and the tests that matter most
here are the ones that hand it a file which does.

    python -m pytest tests/test_bundle_v2.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, bundle, limits, models
from app.context import lineage
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.providers import PromptParts
from app.routers import adventures

SCHEMA = {"player": {"hp": {"min": 0, "max": 100, "initial": 100}}}

# Ten gold a turn, so the script scoreboard is a number that says how many turns
# the story behind it has — which makes an after-snapshot visible from outside.
GOLD_SCRIPT = """
const modifier = (text) => {
  state.gold = (state.gold || 0) + 10;
  return { text };
};
modifier(text);
"""

OPENING = "You enter a cave."


class ScriptedProvider:
    last_usage = None
    replies: list = []
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def generate(self, parts: PromptParts, *, temperature, max_tokens):
        index = min(ScriptedProvider.calls, len(ScriptedProvider.replies) - 1)
        ScriptedProvider.calls += 1
        yield ("text", ScriptedProvider.replies[index])


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="bundle@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    scenario = models.Scenario(user_id=user.id, title="S", stat_schema=SCHEMA)
    setup.add(scenario)
    setup.flush()
    adv = models.Adventure(
        user_id=user.id, title="Cave", scenario_id=scenario.id,
        script_state={}, world_state={"player": {"hp": 100}},
    )
    setup.add(adv)
    setup.flush()
    setup.add(models.Action(adventure_id=adv.id, index=0, type="start", text=OPENING))
    setup.add(models.AdventureScript(
        adventure_id=adv.id, position=0, enabled=True, name="Gold", output_js=GOLD_SCRIPT,
    ))
    setup.commit()
    adv_id, user_id = adv.id, user.id
    setup.close()

    ScriptedProvider.replies = ["A reply."]
    ScriptedProvider.calls = 0
    monkeypatch.setattr(adventures, "OpenAICompatibleProvider", ScriptedProvider)
    monkeypatch.setattr(auth, "resolve_provider_config", lambda s: auth.ProviderConfig(
        "http://fake", "k", "test-model", False))
    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    c.adv_id = adv_id
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        adventures._active_turns.clear()
        Base.metadata.drop_all(bind=engine)


# ------------------------------------------------------------------ helpers

def _play(client, adv_id, text="look around", type="do"):
    r = client.post(f"/api/adventures/{adv_id}/actions", json={"type": type, "text": text})
    assert r.status_code == 200, r.text


def _retry(client, adv_id):
    assert client.post(f"/api/adventures/{adv_id}/retry").status_code == 200


def _export(client, adv_id) -> dict:
    r = client.get(f"/api/adventures/{adv_id}/export")
    assert r.status_code == 200, r.text
    return r.json()


def _import(client, payload):
    return client.post("/api/adventures/import", json=payload)


def _imported(client, payload) -> int:
    r = _import(client, payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _branches(client, adv_id) -> list[dict]:
    r = client.get(f"/api/adventures/{adv_id}/branches")
    assert r.status_code == 200, r.text
    return r.json()


def _switch(client, adv_id, branch_id):
    r = client.post(f"/api/adventures/{adv_id}/branches/{branch_id}/switch")
    assert r.status_code == 200, r.text


def _texts(client, adv_id) -> list[str]:
    return [a["text"] for a in client.get(f"/api/adventures/{adv_id}").json()["actions"]]


def _every_branch_story(client, adv_id) -> list[list[str]]:
    """What each branch tells, in branch order — the whole tree as text."""
    stories = []
    for branch in _branches(client, adv_id):
        _switch(client, adv_id, branch["id"])
        stories.append(_texts(client, adv_id))
    return stories


def _adventure_count() -> int:
    db = SessionLocal()
    try:
        return db.query(models.Adventure).count()
    finally:
        db.close()


def _rows(adv_id) -> list[models.Action]:
    db = SessionLocal()
    try:
        return (
            db.query(models.Action)
            .filter(models.Action.adventure_id == adv_id)
            .order_by(models.Action.branch_id, models.Action.depth,
                      models.Action.variant_index)
            .all()
        )
    finally:
        db.close()


def _branch_rows(adv_id) -> list[models.Branch]:
    db = SessionLocal()
    try:
        return (
            db.query(models.Branch)
            .filter(models.Branch.adventure_id == adv_id)
            .order_by(models.Branch.id)
            .all()
        )
    finally:
        db.close()


def _script_state(adv_id) -> dict:
    db = SessionLocal()
    try:
        return db.get(models.Adventure, adv_id).script_state
    finally:
        db.close()


def _forked_story(client) -> int:
    """A story that went two ways, and stayed both.

        root: start · do · [attempt two] · do · next turn
        fork:              [ATTEMPT ONE] · do · elsewhere

    The *discarded* attempt is the one that gets promoted, because a fork moves
    the take you are leaving for and leaves the line you came from untouched.

    Returns the adventure id, with the head on the fork.
    """
    adv_id = client.adv_id
    ScriptedProvider.replies = ["Attempt one.", "Attempt two.", "Next turn.", "Elsewhere."]
    _play(client, adv_id)
    _retry(client, adv_id)
    _play(client, adv_id, "go deeper")
    discarded = [a.id for a in _rows(adv_id) if a.type == "ai" and not a.live][0]
    assert client.post(f"/api/adventures/{adv_id}/actions/{discarded}/fork").status_code == 200
    _play(client, adv_id, "go sideways")
    return adv_id


# ------------------------------------------------------- the round trip (v2)

def test_a_forked_story_survives_the_round_trip(client):
    """The headline: both futures come back, and both are still readable.

    This is the thing v1 could not do. The check is not "the same rows" — the
    ids are new — but "the same stories", read the way a player reads them: by
    switching to a branch and looking at what it says.
    """
    original = _forked_story(client)
    before = _every_branch_story(client, original)
    assert len(before) == 2, "the fixture forked"
    assert before[0] != before[1], "and the two branches tell different stories"

    copy = _imported(client, _export(client, original))
    assert copy != original
    assert _every_branch_story(client, copy) == before


def test_the_fork_point_comes_back_where_it_was_put(client):
    """`fork_depth` is stored, never inferred — including through a file.

    Inferring it from where two branches' nodes first differ would be a guess
    about how the story was played, and a wrong one the moment an attempt
    happens to repeat its parent's text.
    """
    original = _forked_story(client)
    before = [(b["parent_branch_id"] is None, b["fork_depth"]) for b in _branches(client, original)]

    copy = _imported(client, _export(client, original))
    assert [(b["parent_branch_id"] is None, b["fork_depth"])
            for b in _branches(client, copy)] == before


def test_the_head_comes_back_on_the_branch_it_was_left_on(client):
    original = _forked_story(client)
    head_before = [b["is_head"] for b in _branches(client, original)]
    assert head_before == [False, True], "the fixture left the head on the fork"

    copy = _imported(client, _export(client, original))
    assert [b["is_head"] for b in _branches(client, copy)] == head_before
    # And the tip it sits at is derived from the nodes that arrived, not read
    # out of the file — the bundle never says how deep a branch goes.
    assert _texts(client, copy) == _texts(client, original)


def test_a_switch_in_the_copy_restores_what_that_branch_left_behind(client):
    """The after-snapshots are why the bundle carries them.

    The gold script adds ten a turn, so the scoreboard is a count of the story
    behind it. A bundle that carried the actions but not the outcomes would
    import a tree that reads correctly and switches wrong.
    """
    original = _forked_story(client)
    # One more turn on the fork, so the two tips are genuinely different
    # numbers: played turn for turn, the branches earn the same gold and a
    # switch that restored nothing at all would still look right.
    ScriptedProvider.replies = ["Further still."]
    _play(client, original, "press on")

    per_branch = []
    for branch in _branches(client, original):
        _switch(client, original, branch["id"])
        per_branch.append(_script_state(original).get("gold"))
    assert len(set(per_branch)) == len(per_branch), "the tips are at different totals"

    copy = _imported(client, _export(client, original))
    restored = []
    for branch in _branches(client, copy):
        _switch(client, copy, branch["id"])
        restored.append(_script_state(copy).get("gold"))
    assert restored == per_branch


def test_a_memory_comes_back_on_the_node_it_hangs_off(client):
    """Derived work is addressed by coordinate, so the coordinate is carried."""
    original = _forked_story(client)
    tip = [a for a in _rows(original) if a.live][-1]
    db = SessionLocal()
    try:
        db.add(models.Memory(
            adventure_id=original, text="They met a goblin.",
            source_start=0, source_end=tip.depth,
            branch_id=tip.branch_id, depth=tip.depth,
        ))
        db.commit()
    finally:
        db.close()

    exported = _export(client, original)
    assert [(m["branch"], m["depth"]) for m in exported["memories"]] == [(1, tip.depth)]

    copy = _imported(client, exported)
    db = SessionLocal()
    try:
        memories = db.query(models.Memory).filter(
            models.Memory.adventure_id == copy).all()
        branches = [b.id for b in _branch_rows(copy)]
        assert [(branches.index(m.branch_id), m.depth) for m in memories] == [(1, tip.depth)]
    finally:
        db.close()


# --------------------------------------------------- what is not in the file

def test_the_lineage_is_rebuilt_rather_than_carried(client):
    """A cache of `parent` + `fork_depth` is not a second thing to ship.

    The file says where each branch forked; the ancestry that makes the fork
    readable is computed from that on the way in, capped at the fork exactly as
    `tree.fork` caps it. Shipping the cache too would put two sources of truth
    for one fact in a file anybody can hand-edit.
    """
    original = _forked_story(client)
    exported = _export(client, original)
    assert all("lineage" not in b for b in exported["branches"])

    root, forked = _branch_rows(_imported(client, exported))
    assert root.lineage == [[root.id, None]]
    assert forked.lineage == [[forked.id, None], [root.id, forked.fork_depth]]
    # Which is the arithmetic the reader depends on: the parent is capped one
    # depth short of the attempt that was promoted, so the fork cannot see it.
    assert lineage.entries_of(forked) == [(forked.id, None), (root.id, forked.fork_depth)]


def test_the_legacy_index_is_reissued_so_two_branches_never_share_one(client):
    """`index` is a fact about the adventure, and `depth` is one about a path.

    Two branches have a node at depth 3, so depth cannot be the number
    `max_action_index` hands out next. The import allocates one per turn
    instead: siblings share it, the way SP4 leaves them, and no two coordinates
    do.
    """
    copy = _imported(client, _export(client, _forked_story(client)))
    rows = _rows(copy)
    by_index: dict[int, set] = {}
    for row in rows:
        by_index.setdefault(row.index, set()).add((row.branch_id, row.depth))
    assert all(len(coords) == 1 for coords in by_index.values()), \
        "one index per turn, whatever branch it is on"
    assert len(by_index) == len({(r.branch_id, r.depth) for r in rows})

    # The case that makes the rule necessary: the fork and the line it left
    # both hold a turn at depth 2, and they are not the same turn.
    at_depth_2 = [r for r in rows if r.depth == 2]
    assert len({r.branch_id for r in at_depth_2}) == 2
    assert len({r.index for r in at_depth_2}) == 2, "same depth, different turns"


# ------------------------------------------------------- a file that is wrong

def test_a_node_naming_a_branch_the_file_does_not_list_is_refused(client):
    """Refused, not half-applied. A tree missing a branch is a story that
    silently stops, which is the failure this whole phase exists to end."""
    payload = _export(client, _forked_story(client))
    payload["branches"] = payload["branches"][:1]
    before = _adventure_count()

    r = _import(client, payload)
    assert r.status_code == 400, r.text
    assert "branch" in r.json()["detail"].lower()
    assert _adventure_count() == before, "nothing was created"


def test_a_branch_forking_from_one_listed_after_it_is_refused(client):
    """The ordering rule buys acyclicity for the price of a comparison — and a
    cycle would be an import that never returns rather than one that fails."""
    payload = _export(client, _forked_story(client))
    payload["branches"] = [{"parent": 1, "forkDepth": 0}, {"parent": None, "forkDepth": None}]
    before = _adventure_count()

    r = _import(client, payload)
    assert r.status_code == 400, r.text
    assert _adventure_count() == before


def test_a_fork_with_no_depth_is_refused(client):
    payload = _export(client, _forked_story(client))
    payload["branches"][1].pop("forkDepth")
    before = _adventure_count()

    r = _import(client, payload)
    assert r.status_code == 400, r.text
    assert "depth" in r.json()["detail"].lower()
    assert _adventure_count() == before


def test_more_branches_than_the_cap_is_refused(client, monkeypatch):
    monkeypatch.setattr(auth, "MULTI_USER", True)
    payload = {
        "format": bundle.FORMAT, "title": "Too many",
        "branches": [{"parent": None, "forkDepth": None}]
                    * (limits.MAX_BRANCHES_PER_ADVENTURE + 1),
        "actions": [],
    }
    before = _adventure_count()

    r = _import(client, payload)
    assert r.status_code == 409, r.text
    assert _adventure_count() == before


def test_a_turn_the_file_gives_no_live_attempt_still_tells_one(client):
    """A coordinate with nothing live is a turn no read can see.

    The file is allowed to be wrong about this — it is a text file — so the
    import picks the first attempt rather than importing a story with a hole.
    """
    payload = _export(client, _forked_story(client))
    for node in payload["actions"]:
        node["live"] = False
    copy = _imported(client, payload)

    assert _texts(client, copy), "the story is readable"
    live = [(r.branch_id, r.depth) for r in _rows(copy) if r.live]
    assert len(live) == len(set(live)), "exactly one live attempt per coordinate"
    assert len(live) == len({(r.branch_id, r.depth) for r in _rows(copy)})


# ------------------------------------------------------------- the v1 reader

def test_a_v1_bundle_still_imports(client):
    """The reader stays after the writer goes: those files are already saved."""
    payload = {
        "format": bundle.LEGACY_FORMAT,
        "title": "Old backup",
        "memoryCursor": 0, "summaryCursor": 0,
        "actions": [
            {"index": 0, "type": "start", "text": OPENING},
            {"index": 1, "type": "do", "text": "> You go north."},
            {
                "index": 2, "type": "ai", "text": "Two.",
                "variants": [{"text": "One."}, {"text": "Two."}],
                "variantIndex": 1,
            },
        ],
    }
    copy = _imported(client, payload)

    assert _texts(client, copy) == [OPENING, "> You go north.", "Two."]
    # One branch, and the `variants` array split back into the sibling group it
    # always described.
    assert len(_branches(client, copy)) == 1
    ai = [r for r in _rows(copy) if r.type == "ai"]
    assert [(r.text, r.live) for r in ai] == [("One.", False), ("Two.", True)]
    assert len({(r.branch_id, r.depth) for r in ai}) == 1


def test_a_v1_bundle_with_a_cursor_lands_it_on_a_node(client):
    """v1 counts covered actions; the tree anchors them. The translation needs
    the nodes to exist, so it happens after they are written."""
    payload = {
        "format": bundle.LEGACY_FORMAT, "title": "Old backup",
        "memoryCursor": 2, "summaryCursor": 2,
        "actions": [
            {"index": 0, "type": "start", "text": OPENING},
            {"index": 1, "type": "story", "text": "A corridor."},
            {"index": 2, "type": "story", "text": "A door."},
        ],
    }
    copy = _imported(client, payload)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, copy)
        assert adventure.memory_cursor == 2, "the legacy count is kept as given"
        assert adventure.memory_cursor_branch_id is not None
        assert adventure.memory_cursor_depth == 1, "the second story action"
    finally:
        db.close()


def test_a_v2_bundle_brings_its_anchors_back(client):
    """The other direction: v2 carries the anchor and the count is read off it."""
    original = _forked_story(client)
    tip = [a for a in _rows(original) if a.live][-1]
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, original)
        adventure.memory_cursor_branch_id = tip.branch_id
        adventure.memory_cursor_depth = tip.depth
        db.commit()
    finally:
        db.close()

    exported = _export(client, original)
    assert exported["memoryCursor"] == {"branch": 1, "depth": tip.depth}

    copy = _imported(client, exported)
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, copy)
        branches = [b.id for b in _branch_rows(copy)]
        assert branches.index(adventure.memory_cursor_branch_id) == 1
        assert adventure.memory_cursor_depth == tip.depth
        assert adventure.memory_cursor > 0, "the legacy count was read back off it"
    finally:
        db.close()


def test_a_v1_memory_that_summarises_nothing_lands_on_the_root(client):
    """The import has to answer the question migration 62 answered.

    A v1 file has no depths, and a memory the player typed has no `sourceEnd`
    to derive one from — so it used to come back with a NULL depth, which is the
    exact state SP7 removed from the schema. `Path._entry_clause` compares
    `depth <= max_depth` and a NULL fails it, so the memory would read fine
    until the imported adventure was forked and then vanish from the new branch.
    """
    payload = {
        "format": bundle.LEGACY_FORMAT, "title": "Old backup",
        "memoryCursor": 0, "summaryCursor": 0,
        "actions": [
            {"index": 0, "type": "start", "text": OPENING},
            {"index": 1, "type": "story", "text": "A corridor."},
        ],
        "memories": [
            {"text": "Kira is the innkeeper's daughter"},          # typed
            {"text": "The corridor, summarised", "sourceStart": 1, "sourceEnd": 1},
        ],
    }
    copy = _imported(client, payload)

    db = SessionLocal()
    try:
        rows = {m.text: m for m in db.query(models.Memory)
                .filter(models.Memory.adventure_id == copy).all()}
        assert rows["Kira is the innkeeper's daughter"].depth == 0, (
            "a typed memory anchors at the root, which every branch can see"
        )
        assert rows["The corridor, summarised"].depth == 1, "derived from its range"
        assert all(m.depth is not None for m in rows.values())
        assert all(m.branch_id is not None for m in rows.values())
    finally:
        db.close()


def test_the_action_cap_counts_the_rows_a_v1_file_expands_into(client, monkeypatch):
    """The cap has to count what gets written, not what the file lists.

    A v1 turn carries its retries in a `variants` array, and SP4 made every
    attempt a row — so one entry can become ten. Counting entries lets a file
    inside the cap write a multiple of it, and the body limit is no help: the
    text is tiny, it is the row count that is the cost.
    """
    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(limits, "MAX_ACTIONS_PER_ADVENTURE", 6)
    monkeypatch.setattr(limits, "_BUNDLE_LIST_CAPS",
                        {**limits._BUNDLE_LIST_CAPS, "actions": 6})
    payload = {
        "format": bundle.LEGACY_FORMAT, "title": "Small file, many rows",
        "memoryCursor": 0, "summaryCursor": 0,
        "actions": [{"index": 0, "type": "start", "text": OPENING}] + [
            {
                "index": i, "type": "ai", "text": "Take four.",
                "variants": [{"text": f"Take {n}."} for n in range(4)],
                "variantIndex": 3,
            }
            for i in range(1, 4)
        ],
    }
    assert len(payload["actions"]) <= 6, "the file itself is inside the cap"
    before = _adventure_count()

    r = _import(client, payload)
    assert r.status_code == 409, r.text
    assert _adventure_count() == before, "and nothing was written"


def test_an_unknown_format_is_refused(client):
    r = _import(client, {"format": "ai-dnd-adventure-v3", "title": "From the future"})
    assert r.status_code == 400, r.text
    assert bundle.FORMAT in r.json()["detail"]
