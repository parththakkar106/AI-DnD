"""Guards on how much the database is asked for.

context_snapshot holds the entire assembled prompt for a turn — 163 KB a row
averaged over production, 232 KB on the longest adventure, and 89% of the
database. It used to be pulled for every action on every adventure load and
every turn, to read two tiny things out of it. These tests fail if that
regresses.

Two kinds of guard live here, and both are needed:

* **column guards** assert which columns a statement names. That is the shape
  both of this project's egress blowouts took — one query quietly carrying a
  column nobody read.
* **byte ceilings** assert what a request actually costs. Every column guard
  would still pass if a response grew tenfold within the columns it is allowed
  to read, which is what a story that keeps getting longer does.

    python -m pytest tests/test_egress.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import json

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

# A stand-in for the real thing: the assembled prompt, which is what makes the
# column enormous, plus the small world_state slice the UI actually needs.
BIG_SNAPSHOT = {
    "system": "x" * 20_000,
    "story": "y" * 40_000,
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
            adventure_id=adventure.id, index=i,
            type="ai" if i % 2 else "do", text=f"Action {i}.",
            context_snapshot=BIG_SNAPSHOT,
            world_delta={"delta": {"player.hp": -15},
                         "applied": [{"path": "player.hp", "old": 100, "new": 85}]},
            # Every AI action has been retried twice, so `variants` is carrying
            # weight the list response must not pay for.
            variants=BIG_VARIANTS if i % 2 else None,
            variant_count=len(BIG_VARIANTS) if i % 2 else 0,
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


def test_state_before_and_world_state_before_are_not_fetched_in_bulk(client, sql_log):
    """Both are rollback snapshots, only ever needed for the single action
    being undone or retried."""
    client.get(f"/api/adventures/{client.adv_id}")
    selects = action_selects(sql_log)
    for column in ("state_before", "world_state_before"):
        offenders = [s for s in selects if column in s]
        assert offenders == [], f"{column} was fetched in bulk"


def test_world_changes_still_works_without_the_snapshot(client):
    """The chips under an AI message must survive the snapshot being deferred."""
    r = client.get(f"/api/adventures/{client.adv_id}")
    ai = [a for a in r.json()["actions"] if a["type"] == "ai"]
    assert ai, "fixture should have AI actions"
    assert ai[0]["world_changes"] == [
        {"kind": "stat", "label": "hp", "delta": -15, "value": 85}
    ]


def test_loading_an_adventure_does_not_fetch_variants(client, sql_log):
    """Same failure as context_snapshot, one size down: the payload carries
    only `variant_count`, but loading the column to compute it made every retry
    a permanent tax on every later load of that adventure."""
    r = client.get(f"/api/adventures/{client.adv_id}")
    assert r.status_code == 200, r.text

    selects = action_selects(sql_log)
    assert selects, "expected at least one SELECT against actions"
    offenders = [s for s in selects if "variants" in s]
    assert offenders == [], f"variants was fetched in bulk:\n{offenders[0][:400]}"


def test_variant_count_survives_variants_being_deferred(client):
    """The pager reads this number; it has to be right without the column."""
    r = client.get(f"/api/adventures/{client.adv_id}")
    by_type = {}
    for action in r.json()["actions"]:
        by_type.setdefault(action["type"], []).append(action)
    assert all(a["variant_count"] == len(BIG_VARIANTS) for a in by_type["ai"])
    assert all(a["variant_count"] == 0 for a in by_type["do"])


def test_counting_actions_does_not_name_the_deferred_columns(client, sql_log):
    """A count that wraps the entity select in a subquery names every column in
    the emitted SQL — no bytes come back, but the database still reads them and
    the guard above cannot tell it apart from a real bulk fetch."""
    db = SessionLocal()
    try:
        adventure = db.get(models.Adventure, client.adv_id)
        sql_log.clear()
        assert history.count(adventure) == 12
        counts = [s for s in sql_log if "count" in s.lower()]
        assert counts, "expected a COUNT to be emitted"
        for column in ("context_snapshot", "state_before", "world_state_before", "variants"):
            assert not any(column in s for s in counts), (
                f"{column} is named by the count query:\n{counts[0][:400]}"
            )
    finally:
        db.close()


def test_snapshot_is_still_reachable_on_demand(client):
    """Deferred means lazy, not gone — Insights still gets the full thing."""
    r = client.get(f"/api/adventures/{client.adv_id}")
    action_id = r.json()["actions"][0]["id"]
    r = client.get(f"/api/adventures/{client.adv_id}/actions/{action_id}/context")
    assert r.status_code == 200, r.text
    assert r.json()["system"] == "x" * 20_000


# ------------------------------------------------------------------ backfill

def test_backfill_populates_world_delta_from_existing_snapshots(client):
    """Migration 36 lifts the slice out server-side, without reading the
    snapshots into Python."""
    db = SessionLocal()
    try:
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
    """Migration 37 counts the lists server-side — reading them into Python to
    count them would mean pulling the column across the wire once to stop
    pulling it across forever."""
    db = SessionLocal()
    try:
        db.execute(text("UPDATE actions SET variant_count = 0"))
        db.commit()

        with engine.begin() as conn:
            migrations._backfill_variant_count(conn)

        db.expire_all()
        actions = db.query(models.Action).order_by(models.Action.index).all()
        for action in actions:
            expected = len(BIG_VARIANTS) if action.type == "ai" else 0
            assert action.variant_count == expected, f"action {action.index}"
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
# The tests above assert which *columns* a statement names, which is the shape
# both of this project's egress blowouts took. They would all still pass if a
# response quietly grew tenfold within the columns it is allowed to read — and
# a story that keeps getting longer does exactly that. These put a number on it.
#
# Ceilings are per action rather than absolute, so they mean the same thing
# whatever size the fixture is set to, and they are generous: the point is to
# catch a tenfold regression, not to freeze today's byte count.

ACTIONS_IN_FIXTURE = 12

# 3 kB an action against a real 994 B, measured on production 2026-08-17.
# Anything that pulls a deferred column blows past this by two orders of
# magnitude — see test_the_ceiling_discriminates below.
PAGE_LOAD_BYTES_PER_ACTION = 3_000


@pytest.fixture()
def meter():
    """A byte meter on the shared engine, removed again afterwards.

    Requested *after* `client` in a test's arguments so that building the
    fixture — a write path nobody plays — is not charged to any scope.
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
    """The snapshot is reachable on demand, and that request should pay for
    one row's worth — not the adventure's."""
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


def test_the_ceiling_discriminates(client, meter):
    """A ceiling is only worth having if the thing it excludes would breach it.

    This is the regression the byte tests exist to catch, performed on purpose:
    undefer the snapshot and the same twelve rows cost two orders of magnitude
    more. If this ever stops exceeding the budget, the fixture has gone too
    small for the tests above to mean anything.
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

    assert fetched(meter) > budget * 10, (
        "undeferring the snapshot cost only "
        f"{fetched(meter):,} B — the fixture is too small for the byte "
        "ceilings above to catch anything"
    )
