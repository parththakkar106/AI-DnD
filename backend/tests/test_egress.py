"""Guards on how much the database is asked for.

`context_snapshot` holds the entire assembled prompt for a turn. That is 163 kB
per row averaged over production, 232 kB on the longest adventure, and 89% of the
database. It used to be fetched for every action on every adventure load and
every turn, to read two small values out of it. These tests fail if that
returns.

Two kinds of guard live here, and both are needed:

* Column guards assert which columns a statement names. That is the shape both
  of this project's egress regressions took: one query carrying a column nobody
  read.
* Byte ceilings assert what a request costs. Every column guard would still pass
  if a response grew tenfold within the columns it is allowed to read, which is
  what a story that keeps getting longer does.

    python -m pytest tests/test_egress.py -v
"""
import json
import random

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import undefer

from app import auth, limits, migrations, models
from app.context import history
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from tools import dbmeter
from tools.fakeprose import prose

# A stand-in for the real thing: the assembled prompt, which is what makes the
# column enormous, plus the small world_state slice the UI actually needs.
#
# Varied text, not `"x" * 20_000`. The column is stored compressed now
# by migration 43, and a repeated character compresses about a thousandfold.
# That would make the byte ceilings below pass against a fixture that costs
# nothing, which tests nothing. Prose-shaped filler compresses like the prompts
# it stands in for.
_SNAPSHOT_RNG = random.Random(20_260_817)
BIG_SNAPSHOT = {
    "system": prose(_SNAPSHOT_RNG, 20_000),
    "story": prose(_SNAPSHOT_RNG, 40_000),
    "world_state": {
        "delta": {"player.hp": -15},
        "report": {"applied": [{"path": "player.hp", "old": 100, "new": 85}]},
        "state": {"player": {"hp": 85}},
    },
}

# Retry history: each discarded attempt keeps its full narration, so an action
# retried a few times carries several KB that a list response only ever counts.
BIG_VARIANTS = [
    {"text": "z" * 4_000, "reasoning": None, "script_state": {},
     "created_at": "2026-01-01T00:00:00"}
    for _ in range(3)
]


@pytest.fixture()
def sql_log():
    """Every statement the ORM sends, for asserting on what was selected."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="egress@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))
    adventure = models.Adventure(user_id=user.id, title="Cave", script_state={})
    setup.add(adventure)
    setup.flush()
    for i in range(12):
        setup.add(models.Action(
            adventure_id=adventure.id,
            type="ai" if i % 2 else "do", text=f"Action {i}.",
            context_snapshot=BIG_SNAPSHOT,
            world_delta={"delta": {"player.hp": -15},
                         "applied": [{"path": "player.hp", "old": 100, "new": 85}]},
        ))
    setup.commit()
    adv_id, user_id = adventure.id, user.id
    setup.close()

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
        Base.metadata.drop_all(bind=engine)


def action_selects(statements: list[str]) -> list[str]:
    return [s for s in statements if "FROM actions" in s and s.lstrip().upper().startswith("SELECT")]


# ------------------------------------------------------- the deferred columns

def test_loading_an_adventure_does_not_fetch_context_snapshot(client, sql_log):
    r = client.get(f"/api/adventures/{client.adv_id}")
    assert r.status_code == 200, r.text
    assert len(r.json()["actions"]) == 12

    selects = action_selects(sql_log)
    assert selects, "expected at least one SELECT against actions"
    offenders = [s for s in selects if "context_snapshot" in s]
    assert offenders == [], f"context_snapshot was fetched in bulk:\n{offenders[0][:400]}"


def test_the_state_snapshots_are_not_fetched_in_bulk(client, sql_log):
    """Both are rollback snapshots, only ever needed for the single node being
    undone, retried past or switched to. A page load must pay for neither.
    """
    client.get(f"/api/adventures/{client.adv_id}")
    selects = action_selects(sql_log)
    for column in ("state_after", "world_state_after"):
        offenders = [s for s in selects if column in s]
        assert offenders == [], f"{column} was fetched in bulk"


def test_world_changes_still_works_without_the_snapshot(client):
    """The chips under an AI message must survive the snapshot being deferred."""
    r = client.get(f"/api/adventures/{client.adv_id}")
    ai = [a for a in r.json()["actions"] if a["type"] == "ai"]
    assert ai, "fixture should have AI actions"
    assert ai[0]["world_changes"] == [
        {"kind": "stat", "label": "hp", "delta": -15, "value": 85, "clamped": False}
    ]


def test_counting_actions_does_not_name_the_deferred_columns(client, sql_log):
    """A count that wraps the entity select in a subquery names every column in
    the emitted SQL.

    No bytes come back, but the database still reads them, and the guard above
    cannot distinguish that from a real bulk fetch.
    """
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        sql_log.clear()
        assert history.count(adventure) == 12
        counts = [s for s in sql_log if "count" in s.lower()]
        assert counts, "expected a COUNT to be emitted"
        for column in ("context_snapshot", "state_after", "world_state_after"):
            assert not any(column in s for s in counts), (
                f"{column} is named by the count query:\n{counts[0][:400]}"
            )
    finally:
        db.close()


def test_snapshot_is_still_reachable_on_demand(client):
    """Deferred means lazy rather than absent. Insights still gets the whole
    snapshot."""
    r = client.get(f"/api/adventures/{client.adv_id}")
    action_id = r.json()["actions"][0]["id"]
    r = client.get(f"/api/adventures/{client.adv_id}/actions/{action_id}/context")
    assert r.status_code == 200, r.text
    # Round-tripped through zlib and back to a dict, byte for byte.
    assert r.json()["system"] == BIG_SNAPSHOT["system"]
    assert r.json()["story"] == BIG_SNAPSHOT["story"]


# ------------------------------------------------------------------ backfill

def as_json_snapshot_column(db) -> None:
    """Put actions.context_snapshot back as JSON, the way it was before 43.

    Migration 36 lifts world_delta out of the snapshot with SQL JSON
    functions, so it can only run while the column still *is* JSON. In a real
    upgrade it always is, because 36 runs seven migrations before 43 compresses
    the column into a BLOB. `create_all` builds today's schema, so a test that
    calls that backfill has to rebuild the schema it was written against.
    """
    db.execute(text("ALTER TABLE actions DROP COLUMN context_snapshot"))
    db.execute(text("ALTER TABLE actions ADD COLUMN context_snapshot JSON"))
    db.execute(
        text("UPDATE actions SET context_snapshot = :snapshot"),
        {"snapshot": json.dumps(BIG_SNAPSHOT)},
    )
    db.commit()


def test_backfill_populates_world_delta_from_existing_snapshots(client):
    """Migration 36 lifts the slice out server-side, without reading the
    snapshots into Python."""
    db = SessionLocal()
    try:
        as_json_snapshot_column(db)
        db.execute(text("UPDATE actions SET world_delta = NULL"))
        db.commit()
        assert db.query(models.Action).filter(models.Action.world_delta.isnot(None)).count() == 0

        with engine.begin() as conn:
            migrations._backfill_world_delta(conn)

        db.expire_all()
        actions = db.query(models.Action).all()
        assert all(a.world_delta is not None for a in actions)
        assert actions[0].world_delta["delta"] == {"player.hp": -15}
        assert actions[0].world_delta["applied"] == [
            {"path": "player.hp", "old": 100, "new": 85}
        ]
    finally:
        db.close()


def test_backfill_populates_variant_count_from_existing_variants(client):
    """Migration 37 counts the lists on the server.

    Reading them into Python to count them would fetch the column over the wire
    once in order to stop fetching it on every request.

    Migration 68 drops `variant_count` and 66 drops `variants`, so this test
    puts both columns back before it calls the pass, the same way
    `as_json_snapshot_column` above rebuilds the column migration 36 needs. The
    assertions read raw SQL, because the model no longer has either attribute.
    """
    db = SessionLocal()
    try:
        db.execute(text("ALTER TABLE actions ADD COLUMN variants JSON"))
        db.execute(text(
            "ALTER TABLE actions ADD COLUMN variant_count INTEGER NOT NULL DEFAULT 0"
        ))
        db.execute(
            text("UPDATE actions SET variants = :v WHERE type = 'ai'"),
            {"v": json.dumps(BIG_VARIANTS)},
        )
        db.commit()

        with engine.begin() as conn:
            migrations._backfill_variant_count(conn)

        rows = db.execute(text(
            "SELECT type, variant_count FROM actions ORDER BY id"
        )).all()
        assert rows, "fixture should have actions"
        for kind, count in rows:
            assert count == (len(BIG_VARIANTS) if kind == "ai" else 0)
    finally:
        db.close()


def test_backfill_leaves_actions_without_world_state_alone(client):
    db = SessionLocal()
    try:
        db.execute(text("UPDATE actions SET world_delta = NULL, context_snapshot = '{\"story\": \"s\"}'"))
        db.commit()
        with engine.begin() as conn:
            migrations._backfill_world_delta(conn)
        db.expire_all()
        assert all(a.world_delta is None for a in db.query(models.Action).all())
    finally:
        db.close()


# ---------------------------------------------------------------- byte ceilings
#
# The tests above assert which columns a statement names, which is the shape both
# of this project's egress regressions took. They would all still pass if a
# response grew tenfold within the columns it is allowed to read, and a story
# that keeps getting longer does that. These tests put a number on it.
#
# The ceilings are per action rather than absolute, so they mean the same thing
# whatever size the fixture is, and they are generous. They exist to catch a
# tenfold regression rather than to freeze today's byte count.

ACTIONS_IN_FIXTURE = 12

# 3 kB an action against a real 994 B, measured on production 2026-08-17.
# Anything that pulls a deferred column blows past this by two orders of
# magnitude. See `test_the_ceiling_discriminates` below.
PAGE_LOAD_BYTES_PER_ACTION = 3_000


@pytest.fixture()
def meter():
    """A byte meter on the shared engine, removed again afterwards.

    A test requests this after `client` in its arguments, so that building the
    fixture, which is a write path no player takes, is not charged to any
    scope.
    """
    m = dbmeter.Meter()
    m.attach(engine)
    try:
        yield m
    finally:
        m.detach()


def fetched(meter) -> int:
    return meter.scopes[-1].total.fetched


def test_page_load_stays_under_its_byte_ceiling(client, meter):
    with meter.scope("page load"):
        r = client.get(f"/api/adventures/{client.adv_id}")
    assert r.status_code == 200

    budget = ACTIONS_IN_FIXTURE * PAGE_LOAD_BYTES_PER_ACTION
    assert fetched(meter) < budget, (
        f"page load fetched {fetched(meter):,} B for {ACTIONS_IN_FIXTURE} "
        f"actions, over the {budget:,} B budget"
    )


def test_the_action_list_stays_under_its_byte_ceiling(client, meter):
    with meter.scope("action list"):
        r = client.get(f"/api/adventures/{client.adv_id}/actions")
    assert r.status_code == 200

    budget = ACTIONS_IN_FIXTURE * PAGE_LOAD_BYTES_PER_ACTION
    assert fetched(meter) < budget, (
        f"the action list fetched {fetched(meter):,} B, over {budget:,} B"
    )


def test_reading_one_action_does_not_cost_the_whole_story(client, meter):
    """The snapshot is reachable on demand, and that request pays for one row
    rather than for the whole adventure."""
    db = SessionLocal()
    try:
        action_id = db.query(models.Action.id).order_by(models.Action.id).first()[0]
    finally:
        db.close()

    with meter.scope("one snapshot"):
        r = client.get(f"/api/adventures/{client.adv_id}/actions/{action_id}/context")
    assert r.status_code == 200, r.text

    one_snapshot = len(json.dumps(BIG_SNAPSHOT))
    assert fetched(meter) < one_snapshot * 2, (
        f"fetching one action's snapshot cost {fetched(meter):,} B; one "
        f"snapshot is {one_snapshot:,} B"
    )


def _fat_adventures(user_id: int, count: int = 5, body: int = 20_000) -> None:
    """Adventures whose bodies are heavy and whose index cards are not.

    script_state, world_state and story_summary belong to the play screen. The
    index shows a title, a stamp and a snippet, and used to load all of it.
    """
    db = SessionLocal()
    try:
        for i in range(count):
            db.add(models.Adventure(
                user_id=user_id,
                title=f"Adventure {i}",
                script_state={"log": "s" * body},
                world_state={"player": {"notes": "w" * body}},
                story_summary="y" * body,
                memory="m" * body,
            ))
        db.commit()
    finally:
        db.close()


def test_the_index_does_not_read_the_adventure_body(client, sql_log):
    db = SessionLocal()
    try:
        user_id = db.query(models.User.id).first()[0]
    finally:
        db.close()
    _fat_adventures(user_id)

    r = client.get("/api/adventures")
    assert r.status_code == 200
    assert len(r.json()) == 6  # the fixture's one, plus five

    listing = [
        s for s in sql_log
        if "FROM adventures" in s and s.lstrip().upper().startswith("SELECT")
    ]
    assert listing, "expected a listing query"
    for column in ("script_state", "world_state", "story_summary", "memory",
                   "authors_note", "ai_instructions", "placeholders"):
        assert not any(column in s for s in listing), (
            f"the index read adventures.{column}, which nothing on that "
            f"screen displays"
        )


def test_the_index_stays_under_its_byte_ceiling(client, meter):
    db = SessionLocal()
    try:
        user_id = db.query(models.User.id).first()[0]
    finally:
        db.close()
    _fat_adventures(user_id)

    with meter.scope("index"):
        r = client.get("/api/adventures")
    assert r.status_code == 200

    # Six adventures carrying 80 kB of body each. A card is a title, a stamp
    # and a 220-character snippet; 4 kB apiece is already generous.
    budget = 6 * 4_000
    assert fetched(meter) < budget, (
        f"the index fetched {fetched(meter):,} B for six adventures, over "
        f"{budget:,} B — it is reading the bodies again"
    )


def test_the_ceiling_discriminates(client, meter):
    """A ceiling is only worth having if the thing it excludes would breach it.

    This is the regression the byte tests exist to catch, performed on purpose:
    undefer the snapshot and the same twelve rows cost several times the whole
    budget. If this ever stops exceeding it, the fixture has gone too small for
    the tests above to mean anything.

    The margin used to be a hundredfold and is now about six. The guard has not
    weakened. Migration 43 compresses the column, and the fixture text is
    prose-shaped, so it compresses like a real prompt rather than like a repeated
    character.
    """
    budget = ACTIONS_IN_FIXTURE * PAGE_LOAD_BYTES_PER_ACTION
    db = SessionLocal()
    try:
        with meter.scope("undeferred"):
            rows = (
                db.query(models.Action)
                .options(undefer(models.Action.context_snapshot))
                .all()
            )
            assert len(rows) == ACTIONS_IN_FIXTURE
    finally:
        db.close()

    assert fetched(meter) > budget * 3, (
        "undeferring the snapshot cost only "
        f"{fetched(meter):,} B against a {budget:,} B budget — the fixture is "
        "too small for the byte ceilings above to catch anything"
    )
