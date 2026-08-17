"""Phase 14 SP1 — every existing adventure becomes a tree with one branch.

The migration this file watches is the one that cannot be re-run: it reads
`index` and writes `depth`, and from SP2 on the reads follow `depth`. If it
mis-maps a row, that row does not error — it *disappears from the story*, which
is why the assertions here are about every row rather than about a sample.

The fixture is a genuine **schema 45** database, not a current one with an old
stamp. `create_all` always builds the current schema, so the three tables the
tree touches are dropped and rebuilt from frozen pre-tree DDL below; the
migration then runs its real ALTERs against them, including the one that adds a
foreign key. A pre-migration database built any other way (stamp rewound,
columns left in place) would quietly skip the DDL and test half the change.

    python -m pytest tests/test_tree_migration.py -v
"""
import json
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
from sqlalchemy import text

from app import auth, compression, limits, migrations, models, tree
from app.context import history
from app.database import Base, SessionLocal, engine, get_db
from app.main import app

# The three tables as they stood at schema 45, frozen. This is a snapshot of a
# past schema and must NOT be updated to track models.py — the whole point is
# that it lacks what SP1 adds. SQLite spelling only; the migration's Postgres
# half is exercised against a real server at deploy time (see plan/14).
PRE_TREE_DDL = (
    """
    CREATE TABLE adventures (
        id INTEGER NOT NULL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        scenario_id INTEGER,
        title VARCHAR(200) NOT NULL DEFAULT 'Untitled Adventure',
        memory TEXT NOT NULL DEFAULT '',
        authors_note TEXT NOT NULL DEFAULT '',
        ai_instructions TEXT NOT NULL DEFAULT '',
        story_summary TEXT NOT NULL DEFAULT '',
        script_state JSON NOT NULL DEFAULT '{}',
        world_state JSON NOT NULL DEFAULT '{}',
        placeholders JSON,
        auto_summarize BOOLEAN NOT NULL DEFAULT 0,
        memory_bank_enabled BOOLEAN NOT NULL DEFAULT 0,
        memory_cursor INTEGER NOT NULL DEFAULT 0,
        summary_cursor INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE actions (
        id INTEGER NOT NULL PRIMARY KEY,
        adventure_id INTEGER NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
        "index" INTEGER NOT NULL,
        type VARCHAR(20) NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        reasoning TEXT,
        context_snapshot BLOB,
        world_delta JSON,
        state_before JSON,
        world_state_before JSON,
        variants JSON,
        variant_count INTEGER NOT NULL DEFAULT 0,
        variant_index INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME
    )
    """,
    """
    CREATE TABLE memories (
        id INTEGER NOT NULL PRIMARY KEY,
        adventure_id INTEGER NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
        text TEXT NOT NULL DEFAULT '',
        embedding_blob BLOB,
        source_start INTEGER,
        source_end INTEGER,
        embedded BOOLEAN NOT NULL DEFAULT 0,
        pinned BOOLEAN NOT NULL DEFAULT 0,
        forgotten BOOLEAN NOT NULL DEFAULT 0,
        use_count INTEGER NOT NULL DEFAULT 0,
        last_used_at DATETIME,
        created_at DATETIME
    )
    """,
)

# The story of adventure "Gapped": index 3 is missing, because deleting a middle
# action never renumbered the ones after it. The gap has to survive as a gap.
GAPPED_INDEXES = (0, 1, 2, 4)
STRAIGHT_INDEXES = (0, 1)
# "Blank" holds an action whose text is nothing but whitespace. It is a row of
# the adventure but not of the *story*, so a cursor counting covered actions
# never counted it — and migration 56 has to skip it the same way, using a
# frozen copy of the story-text predicate. This is the one duplicated
# definition in the change, so it gets the one case that can tell.
BLANK_INDEXES = (0, 1, 2, 3)
BLANK_AT = 2


@pytest.fixture()
def pre_tree():
    """A schema-45 database with three adventures in it, returned as the ids
    (gapped, straight, empty) their stories were written under."""
    # Every test in this module shares one temp file, and a setup that fails
    # before its yield never reaches a teardown — so start from empty rather
    # than from whatever the last one left.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        # `branches` and the six new columns never existed at 45. Dropping the
        # tables is the only way to lose the columns: SQLite refuses to drop a
        # column a foreign key names, which is exactly the case for branch_id.
        for table in ("actions", "memories", "branches", "adventures"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        for ddl in PRE_TREE_DDL:
            conn.execute(text(ddl))
        conn.execute(text(
            "INSERT INTO users (id, email, is_guest, created_at, demo_turns_used, "
            "demo_turns_date) VALUES (1, 'v45@example.com', 0, CURRENT_TIMESTAMP, 0, '')"
        ))

        # The cursors as schema 45 held them: counts of covered story actions.
        # Gapped's story is 0,1,2,4 — so "3 covered" is the node at depth 2 and
        # "4 covered" is the node at depth 4, which is the whole reason a count
        # and a depth are not the same number. Straight is caught up past its
        # own end (5 covered, 2 actions), which is a state the older rule left
        # behind and the clamp used to paper over every post-turn pass.
        cursors_at = {"Gapped": (3, 4), "Straight": (5, 0), "Empty": (0, 0),
                      "Blank": (3, 0)}
        ids = {}
        for name in ("Gapped", "Straight", "Empty", "Blank"):
            conn.execute(text(
                "INSERT INTO adventures (user_id, title, memory_cursor, summary_cursor) "
                "VALUES (1, :title, :mc, :sc)"
            ), {"title": name, "mc": cursors_at[name][0], "sc": cursors_at[name][1]})
            ids[name] = conn.execute(text(
                "SELECT id FROM adventures WHERE title = :title"
            ), {"title": name}).scalar()

        for adventure_id, indexes in (
            (ids["Gapped"], GAPPED_INDEXES),
            (ids["Straight"], STRAIGHT_INDEXES),
            (ids["Blank"], BLANK_INDEXES),
        ):
            for index in indexes:
                blank = adventure_id == ids["Blank"] and index == BLANK_AT
                conn.execute(text(
                    'INSERT INTO actions (adventure_id, "index", type, text) '
                    "VALUES (:a, :i, :t, :x)"
                ), {"a": adventure_id, "i": index,
                    "t": "start" if index == 0 else "do",
                    "x": " \n\t " if blank else f"Turn {index}."})

        # One memory that summarised a block of story, and one written by hand,
        # which summarised nothing and so belongs to no node.
        conn.execute(text(
            "INSERT INTO memories (adventure_id, text, source_start, source_end) "
            "VALUES (:a, 'The gate opened.', 0, 1)"
        ), {"a": ids["Gapped"]})
        conn.execute(text(
            "INSERT INTO memories (adventure_id, text) VALUES (:a, 'Hand-written.')"
        ), {"a": ids["Gapped"]})

        conn.execute(text("PRAGMA user_version = 45"))

    try:
        yield ids
    finally:
        Base.metadata.drop_all(bind=engine)


def rows(sql: str, **params) -> list[tuple]:
    with engine.begin() as conn:
        return conn.execute(text(sql), params).all()


def scalar(sql: str, **params):
    with engine.begin() as conn:
        return conn.execute(text(sql), params).scalar()


# ------------------------------------------------------- the migration itself

def test_the_stamp_reaches_the_current_version(pre_tree):
    migrations.bootstrap(engine)
    assert scalar("PRAGMA user_version") == migrations.LATEST_VERSION


def test_every_action_lands_on_its_adventure_root_branch(pre_tree):
    before = scalar("SELECT count(*) FROM actions")

    migrations.bootstrap(engine)

    assert scalar("SELECT count(*) FROM actions") == before, "the migration lost a row"
    assert scalar("SELECT count(*) FROM actions WHERE branch_id IS NULL") == 0
    assert scalar("SELECT count(*) FROM actions WHERE depth IS NULL") == 0
    # Each action's branch belongs to that action's own adventure. A branch
    # clause that forgot its adventure would still look right on a database
    # holding one, which is why the fixture holds three.
    mismatched = scalar("""
        SELECT count(*) FROM actions a JOIN branches b ON b.id = a.branch_id
        WHERE b.adventure_id != a.adventure_id
    """)
    assert mismatched == 0


def test_depth_is_the_old_index_gaps_included(pre_tree):
    migrations.bootstrap(engine)

    assert rows('SELECT "index", depth FROM actions WHERE depth != "index"') == []
    depths = [
        row[0] for row in rows(
            "SELECT depth FROM actions WHERE adventure_id = :a ORDER BY depth",
            a=pre_tree["Gapped"],
        )
    ]
    # 3 is still missing. Renumbering here would silently move every cursor
    # pointing past the gap, and the reads only need the order, not density.
    assert depths == list(GAPPED_INDEXES)


def test_one_root_branch_per_adventure_with_its_own_lineage(pre_tree):
    migrations.bootstrap(engine)

    branches = rows(
        "SELECT id, adventure_id, parent_branch_id, fork_depth, lineage FROM branches"
    )
    assert len(branches) == 4, "one branch per adventure, including the empty one"
    for branch_id, _adventure_id, parent, fork_depth, lineage in branches:
        assert parent is None, "a migrated branch is a root; nothing forked yet"
        assert fork_depth is None
        # The whole story, uncapped: one entry, itself, no ceiling.
        assert json.loads(lineage) == [[branch_id, None]]


def test_the_head_points_at_the_tip_of_the_root_branch(pre_tree):
    migrations.bootstrap(engine)

    heads = dict(rows("SELECT title, head_depth FROM adventures"))
    assert heads["Gapped"] == max(GAPPED_INDEXES)
    assert heads["Straight"] == max(STRAIGHT_INDEXES)
    # No actions, no tip. -1 keeps "the next node goes at head_depth + 1" true
    # without a special case anywhere else.
    assert heads["Empty"] == tree.NO_DEPTH
    assert scalar("SELECT count(*) FROM adventures WHERE head_branch_id IS NULL") == 0
    dangling = scalar("""
        SELECT count(*) FROM adventures a
        WHERE NOT EXISTS (
            SELECT 1 FROM branches b
            WHERE b.id = a.head_branch_id AND b.adventure_id = a.id
        )
    """)
    assert dangling == 0, "a head pointing outside its own adventure"


def test_memories_attach_to_the_node_they_summarised(pre_tree):
    migrations.bootstrap(engine)

    summarised = rows(
        "SELECT source_end, depth, branch_id FROM memories WHERE source_end IS NOT NULL"
    )
    assert summarised, "the fixture is supposed to have one"
    for source_end, depth, branch_id in summarised:
        assert depth == source_end, "the memory hangs off the last action it covered"
        assert branch_id is not None

    # A hand-written memory has no node: it gets a branch, but no depth, which
    # SP3 reads as belonging to the adventure rather than to a path.
    manual = rows("SELECT depth, branch_id FROM memories WHERE source_end IS NULL")
    assert manual and all(depth is None and branch is not None for depth, branch in manual)


def test_the_cursors_become_the_nodes_they_named(pre_tree):
    """SP3, migration 56. A count of covered actions and a depth are different
    numbers the moment the story has a gap in it, which every adventure anyone
    has ever deleted from does."""
    migrations.bootstrap(engine)

    def marks(title):
        [row] = rows(
            "SELECT memory_cursor_depth, summary_cursor_depth, "
            "memory_cursor_branch_id, summary_cursor_branch_id "
            "FROM adventures WHERE title = :t", t=title
        )
        return row

    # Gapped's story is 0,1,2,4. "3 covered" is the *third* action, at depth 2 —
    # reading the count as a depth would have handed the summarizer node 3,
    # which does not exist, and quietly skipped node 4 forever.
    memory_depth, summary_depth, memory_branch, summary_branch = marks("Gapped")
    assert (memory_depth, summary_depth) == (2, 4)
    root = scalar(
        "SELECT id FROM branches WHERE adventure_id = "
        "(SELECT id FROM adventures WHERE title = 'Gapped')"
    )
    assert memory_branch == summary_branch == root

    # Straight was caught up under the older rule: 5 covered, 2 actions. There
    # is no fifth node to name, and the number meant "caught up", so it lands
    # on the tip rather than on nothing.
    memory_depth, summary_depth, _, summary_branch = marks("Straight")
    assert memory_depth == 1
    assert (summary_depth, summary_branch) == (migrations.NO_DEPTH, None)

    # Nothing covered stays nothing covered, and names no branch.
    assert marks("Empty") == (migrations.NO_DEPTH, migrations.NO_DEPTH, None, None)

    # A whitespace-only action is a row but not a story action, so it was never
    # counted — "3 covered" of 0,1,[blank],3 is the node at depth 3, not 2. The
    # migration's copy of the story-text predicate is the only place that rule
    # is written twice, so this is the case that catches it drifting.
    assert marks("Blank")[0] == 3

    # The legacy columns are left exactly as they were: a rolled-back build
    # reads them, and this migration is not the one that drops them.
    assert rows(
        "SELECT memory_cursor, summary_cursor FROM adventures ORDER BY title"
    ) == [(3, 0), (0, 0), (3, 4), (5, 0)]  # Blank, Empty, Gapped, Straight


def test_the_branch_clause_index_exists(pre_tree):
    """SP2's reads are only cheap if this exists — and `create_all` does not add
    an index to a table it did not create, which is what migration 52 is for."""
    migrations.bootstrap(engine)

    assert scalar(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type = 'index' AND name = 'ix_actions_branch_depth'"
    ) == 1


def test_running_it_again_changes_nothing(pre_tree):
    migrations.bootstrap(engine)
    snapshot = (
        rows("SELECT id, branch_id, depth FROM actions ORDER BY id"),
        rows("SELECT id, adventure_id, lineage FROM branches ORDER BY id"),
        rows("SELECT id, head_branch_id, head_depth, memory_cursor_branch_id, "
             "memory_cursor_depth, summary_cursor_branch_id, summary_cursor_depth "
             "FROM adventures ORDER BY id"),
        rows("SELECT id, branch_id, depth FROM memories ORDER BY id"),
    )

    # Twice through the deploy path, then the data pass on its own — the stamp
    # stops the first, the NULL guards stop the second, and a migration that
    # only survives because of the stamp is one bad rescue away from doubling
    # every branch.
    migrations.bootstrap(engine)
    with engine.begin() as conn:
        migrations._backfill_tree(conn)
        migrations._backfill_cursor_anchors(conn)

    assert (
        rows("SELECT id, branch_id, depth FROM actions ORDER BY id"),
        rows("SELECT id, adventure_id, lineage FROM branches ORDER BY id"),
        rows("SELECT id, head_branch_id, head_depth, memory_cursor_branch_id, "
             "memory_cursor_depth, summary_cursor_branch_id, summary_cursor_depth "
             "FROM adventures ORDER BY id"),
        rows("SELECT id, branch_id, depth FROM memories ORDER BY id"),
    ) == snapshot


# -------------------------------------------------- rows written *after* it

@pytest.fixture()
def client(monkeypatch):
    """The app on a migrated database, so new rows go through the real writers.

    Everything the migration fixes is only half the job: no migration will ever
    visit a row written after it ran, and a row without a branch is a row no
    read can see.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="writer@example.com")
    setup.add(user)
    setup.flush()
    scenario = models.Scenario(user_id=user.id, title="S", prompt="You enter a cave.")
    setup.add(scenario)
    setup.commit()
    user_id, scenario_id = user.id, scenario.id
    setup.close()

    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    c.scenario_id = scenario_id
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def test_a_new_adventure_gets_a_branch_and_its_opening_sits_on_it(client):
    response = client.post("/api/adventures", json={"scenario_id": client.scenario_id})
    assert response.status_code == 201
    adventure_id = response.json()["id"]

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, adventure_id)
        branch = db.query(models.Branch).filter_by(adventure_id=adventure_id).one()
        assert adventure.head_branch_id == branch.id
        assert branch.lineage == [[branch.id, None]]
        opening = db.query(models.Action).filter_by(adventure_id=adventure_id).one()
        assert (opening.branch_id, opening.depth) == (branch.id, 0)
        assert adventure.head_depth == 0
    finally:
        db.close()


def test_a_blank_adventure_has_a_branch_before_anything_is_played(client):
    adventure_id = client.post("/api/adventures", json={}).json()["id"]

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, adventure_id)
        assert adventure.head_branch_id is not None
        assert adventure.head_depth == tree.NO_DEPTH
    finally:
        db.close()


def test_a_hand_written_memory_gets_a_branch_but_no_depth(client):
    adventure_id = client.post("/api/adventures", json={}).json()["id"]

    created = client.post(
        f"/api/adventures/{adventure_id}/memories", json={"text": "Remember the gate."}
    )
    assert created.status_code == 201

    db = SessionLocal()
    try:
        memory = db.query(models.Memory).filter_by(adventure_id=adventure_id).one()
        assert memory.branch_id is not None
        assert memory.depth is None
    finally:
        db.close()


def test_deleting_a_branch_takes_its_nodes_with_it(client):
    """`ON DELETE CASCADE` on both `branch_id` columns, so the database removes a
    branch's nodes rather than any code remembering to. SP7 ships delete-a-branch
    on top of exactly this, and nothing else has to load a branch to do it."""
    adventure_id = client.post(
        "/api/adventures", json={"scenario_id": client.scenario_id}
    ).json()["id"]

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, adventure_id)
        memory = models.Memory(
            adventure_id=adventure_id, text="m", source_start=0, source_end=0
        )
        tree.place_memory(db, adventure, memory)
        db.add(memory)
        db.commit()
        branch_id = adventure.head_branch_id

        db.execute(
            models.Branch.__table__.delete().where(models.Branch.id == branch_id)
        )
        db.commit()
        assert db.query(models.Action).filter_by(adventure_id=adventure_id).count() == 0
        assert db.query(models.Memory).filter_by(adventure_id=adventure_id).count() == 0
    finally:
        db.close()


def test_deleting_an_adventure_takes_its_branch_with_it(client):
    adventure_id = client.post(
        "/api/adventures", json={"scenario_id": client.scenario_id}
    ).json()["id"]

    assert client.delete(f"/api/adventures/{adventure_id}").status_code == 204

    db = SessionLocal()
    try:
        assert db.query(models.Branch).filter_by(adventure_id=adventure_id).count() == 0
    finally:
        db.close()


def test_deleting_the_newest_action_moves_the_head_back(client):
    """The head is a cache, and a cache that only ever moves forward is wrong
    the first time someone undoes a turn."""
    adventure_id = client.post(
        "/api/adventures", json={"scenario_id": client.scenario_id}
    ).json()["id"]

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, adventure_id)
        extra = models.Action(adventure_id=adventure_id, index=1, type="do", text="Look.")
        tree.place_action(db, adventure, extra)
        db.add(extra)
        db.commit()
        assert adventure.head_depth == 1
        action_id = extra.id
    finally:
        db.close()

    assert client.delete(
        f"/api/adventures/{adventure_id}/actions/{action_id}"
    ).status_code == 204

    db = SessionLocal()
    try:
        assert db.get(models.Adventure, adventure_id).head_depth == 0
    finally:
        db.close()


# ---------------------------------------- SP4: variants become sibling rows

# One turn's retry history as schema 45 stored it: a JSON array on the AI row,
# with `variant_index` naming the entry `text` mirrors. The live one is
# deliberately not the last written — a migration that assumed it was would
# look right on every fixture where the player never went back.
RETRY_VARIANTS = [
    {"text": "Attempt one.", "reasoning": None,
     "script_state": {"gold": 10}, "created_at": "2026-01-01T00:00:00",
     "raw_output": "Attempt one.",
     "world_state": {"delta": {"player.hp": -5},
                     "report": {"applied": [{"path": "player.hp", "old": 100, "new": 95}]},
                     "state": {"player": {"hp": 95}}}},
    {"text": "Attempt two.", "reasoning": "thinking",
     "script_state": {"gold": 20}, "created_at": "2026-01-01T00:01:00",
     "raw_output": "Attempt two.",
     "world_state": {"delta": {"player.hp": -40},
                     "report": {"applied": [{"path": "player.hp", "old": 100, "new": 60}]},
                     "state": {"player": {"hp": 60}}}},
    {"text": "Attempt three.", "reasoning": None,
     "script_state": {"gold": 30}, "created_at": "2026-01-01T00:02:00",
     "raw_output": "Attempt three."},
]
LIVE_VARIANT = 1

# The whole turn's assembled prompt, stored once. The attempts differ only in
# the three slices above, which is the arrangement SP4 has to preserve — giving
# each sibling a copy of this would multiply the biggest column in the database
# by the retry count.
RETRY_SNAPSHOT = {
    "sections": [{"label": "history", "text": "A long prompt.", "tokens": 4}],
    "prompt": {"system": "S", "story": "A long prompt."},
    "raw_output": "Attempt two.",
    "script": {"logs": []},
    "world_state": RETRY_VARIANTS[LIVE_VARIANT]["world_state"],
}


@pytest.fixture()
def pre_split():
    """A schema-45 adventure with one retried turn, plus a plain turn each side.

    Separate from `pre_tree` so SP1's assertions keep counting what they were
    written to count. The story is: 0 start, 1 do, 2 ai (three attempts), 3 do,
    and the adventure's live state is the one attempt 1 produced.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table in ("actions", "memories", "branches", "adventures"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        for ddl in PRE_TREE_DDL:
            conn.execute(text(ddl))
        conn.execute(text(
            "INSERT INTO users (id, email, is_guest, created_at, demo_turns_used, "
            "demo_turns_date) VALUES (1, 'v45@example.com', 0, CURRENT_TIMESTAMP, 0, '')"
        ))
        conn.execute(text(
            "INSERT INTO adventures (user_id, title, script_state, world_state) "
            "VALUES (1, 'Retried', :script, :world)"
        ), {"script": json.dumps({"gold": 20}),
            "world": json.dumps({"player": {"hp": 60}})})
        adventure_id = conn.execute(
            text("SELECT id FROM adventures WHERE title = 'Retried'")
        ).scalar()
        # `state_before` on each row: the scoreboard as that action found it.
        # SP4 reads them one row along to build the `state_after` pair.
        for index, kind, before in (
            (0, "start", None), (1, "do", {"gold": 0}),
            (2, "ai", {"gold": 0}), (3, "do", {"gold": 20}),
        ):
            conn.execute(text(
                'INSERT INTO actions (adventure_id, "index", type, text, reasoning, '
                "state_before, context_snapshot, variants, variant_count, variant_index) "
                "VALUES (:a, :i, :t, :x, :r, :sb, :cs, :v, :vc, :vi)"
            ), {
                "a": adventure_id, "i": index, "t": kind,
                "x": RETRY_VARIANTS[LIVE_VARIANT]["text"] if kind == "ai" else f"Turn {index}.",
                "r": RETRY_VARIANTS[LIVE_VARIANT]["reasoning"] if kind == "ai" else None,
                "sb": None if before is None else json.dumps(before),
                "cs": compression.pack(RETRY_SNAPSHOT) if kind == "ai" else None,
                "v": json.dumps(RETRY_VARIANTS) if kind == "ai" else None,
                "vc": len(RETRY_VARIANTS) if kind == "ai" else 0,
                "vi": LIVE_VARIANT if kind == "ai" else 0,
            })
        conn.execute(text("PRAGMA user_version = 45"))
    try:
        yield adventure_id
    finally:
        Base.metadata.drop_all(bind=engine)


def _attempts(adventure_id) -> list[tuple]:
    return rows(
        "SELECT variant_index, text, live, variant_count FROM actions "
        'WHERE adventure_id = :a AND "index" = 2 ORDER BY variant_index',
        a=adventure_id,
    )


def test_each_attempt_becomes_a_row_at_the_turns_coordinate(pre_split):
    migrations.bootstrap(engine)

    assert _attempts(pre_split) == [
        (0, "Attempt one.", 0, 3),
        (1, "Attempt two.", 1, 3),
        (2, "Attempt three.", 0, 3),
    ]
    # One turn, one coordinate: the siblings share a branch and a depth, and
    # keep the legacy index that says which turn they are all takes on.
    coordinates = rows(
        'SELECT DISTINCT branch_id, depth FROM actions WHERE adventure_id = :a '
        'AND "index" = 2', a=pre_split,
    )
    assert len(coordinates) == 1
    # ...and the rest of the story is untouched, still one row per turn.
    assert scalar("SELECT count(*) FROM actions WHERE adventure_id = :a", a=pre_split) == 6


def test_the_live_attempt_is_the_one_the_row_was_mirroring(pre_split):
    """`variant_index` is the only record of which take the player was reading,
    and it survives as the `live` flag. Guessing "the newest" instead would
    silently rewrite the story of anyone who had paged back."""
    migrations.bootstrap(engine)

    live = rows(
        "SELECT text FROM actions WHERE adventure_id = :a AND live = 1 "
        'AND "index" = 2', a=pre_split,
    )
    assert live == [("Attempt two.",)]


def test_the_prompt_stays_on_the_live_attempt_and_nowhere_else(pre_split):
    migrations.bootstrap(engine)

    holders = []
    for variant_index, snapshot in rows(
        'SELECT variant_index, context_snapshot FROM actions WHERE adventure_id = :a '
        'AND "index" = 2 ORDER BY variant_index', a=pre_split,
    ):
        stored = compression.unpack(snapshot) if snapshot else {}
        if "sections" in stored:
            holders.append(variant_index)
        else:
            # A superseded attempt keeps only what was its own.
            assert set(stored) <= set(migrations._ATTEMPT_KEYS)
    assert holders == [LIVE_VARIANT]


def test_each_attempt_keeps_the_outcome_it_produced(pre_split):
    migrations.bootstrap(engine)

    parsed = [
        (i, json.loads(state), json.loads(world) if world else None)
        for i, state, world in rows(
            "SELECT variant_index, state_after, world_state_after FROM actions "
            'WHERE adventure_id = :a AND "index" = 2 ORDER BY variant_index',
            a=pre_split,
        )
    ]
    assert [(i, s) for i, s, _ in parsed] == [
        (0, {"gold": 10}), (1, {"gold": 20}), (2, {"gold": 30})
    ]
    assert parsed[0][2] == {"player": {"hp": 95}}
    assert parsed[1][2] == {"player": {"hp": 60}}
    # Attempt three recorded no world state — an adventure with no RPG layer,
    # or a take made before the column existed. It stays NULL rather than
    # borrowing a neighbour's, and switching to it leaves the RPG layer alone:
    # exactly what `apply_variant` did with an entry that had no world state.
    assert parsed[2][2] is None


def test_state_after_is_the_state_before_of_the_turn_in_front(pre_split):
    migrations.bootstrap(engine)

    after = dict(rows(
        'SELECT "index", state_after FROM actions WHERE adventure_id = :a '
        "AND live = 1 ORDER BY depth", a=pre_split,
    ))
    # Action 1's outcome is action 2's starting position, exactly.
    assert json.loads(after[1]) == {"gold": 0}
    # The tip has nothing in front of it, so what it left behind is what the
    # adventure is carrying now.
    assert json.loads(after[3]) == {"gold": 20}


def test_the_split_survives_being_run_again(pre_split):
    migrations.bootstrap(engine)
    snapshot = _attempts(pre_split)
    before = scalar("SELECT count(*) FROM actions")

    migrations.bootstrap(engine)
    with engine.begin() as conn:
        migrations._backfill_state_after(conn)
        migrations._split_variants_into_siblings(conn)

    assert scalar("SELECT count(*) FROM actions") == before, "attempts were duplicated"
    assert _attempts(pre_split) == snapshot


def test_the_migrated_story_reads_back_as_one_turn(pre_split):
    """The point of all of it: the reads see a four-action story, not six."""
    migrations.bootstrap(engine)

    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, pre_split)
        assert [a.text for a in history.story_actions(adventure)] == [
            "Turn 0.", "Turn 1.", "Attempt two.", "Turn 3.",
        ]
        assert history.count(adventure) == 4
    finally:
        db.close()
