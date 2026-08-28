"""Visit analytics: app/analytics.py and the two endpoints in front of it.

This file tests three things, and the rest is arithmetic. The counters must
survive the buffer/UPSERT round trip: a flush adds to what is already
stored instead of replacing it, or every number would show only the last
minute. The funnel counts people rather than clicks, which is the only
reason the visitor-day table exists. The gate holds: a stranger cannot read
the dashboard, and cannot inflate what it reports beyond hitting the page.

    python -m pytest tests/test_analytics.py -v
"""
from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import analytics, auth, limits, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app


@pytest.fixture(autouse=True)
def clean_buffer():
    """The buffer is process-wide, so a test that leaves counts in it would
    show up inside the next one's flush."""
    analytics._counts.clear()
    analytics._visits.clear()
    analytics._labels_seen.clear()
    yield
    analytics._counts.clear()
    analytics._visits.clear()
    analytics._labels_seen.clear()


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def counter(db, metric, label):
    row = (
        db.query(models.AnalyticsDaily)
        .filter_by(metric=metric, label=label)
        .one_or_none()
    )
    return row.hits if row else 0


def make_user(db, email=None):
    user = models.User(is_guest=email is None, email=email)
    db.add(user)
    db.commit()
    return user


# ---------- The buffer and its flush ----------

def test_counts_accumulate_across_flushes(db):
    analytics.record(analytics.M_PAGE, "/")
    analytics.record(analytics.M_PAGE, "/")
    analytics.flush(db)
    analytics.record(analytics.M_PAGE, "/")
    analytics.flush(db)
    # The second flush has to find the existing row and add to it. Replacing it
    # would leave every counter showing only the newest minute of traffic.
    assert counter(db, analytics.M_PAGE, "/") == 3


def test_flush_is_a_no_op_when_nothing_happened(db):
    analytics.flush(db)
    assert db.query(models.AnalyticsDaily).count() == 0


def test_a_failed_flush_keeps_the_counts(db, monkeypatch):
    analytics.record(analytics.M_PAGE, "/")
    monkeypatch.setattr(analytics, "_write_counts", lambda *a: 1 / 0)
    analytics.flush(db)  # must not raise
    monkeypatch.undo()
    analytics.flush(db)
    assert counter(db, analytics.M_PAGE, "/") == 1


def test_label_cardinality_is_capped(db):
    for i in range(analytics.MAX_LABELS_PER_METRIC + 25):
        analytics.record(analytics.M_REFERRER, f"host{i}.example")
    analytics.flush(db)
    labels = db.query(models.AnalyticsDaily).filter_by(metric=analytics.M_REFERRER).count()
    # Everything past the cap is folded into one bucket, so a referrer flood
    # cannot create unlimited rows.
    assert labels == analytics.MAX_LABELS_PER_METRIC + 1
    assert counter(db, analytics.M_REFERRER, analytics.OTHER) == 25


# ---------- Visitors ----------

def test_visitor_id_is_stable_and_keyed(db, monkeypatch):
    user = make_user(db)
    handle = analytics.visitor_id(user)
    assert handle == analytics.visitor_id(user)               # a returning visitor
    assert handle != analytics.visitor_id(make_user(db))      # is still one visitor
    assert len(handle) == 32 and int(handle, 16) >= 0         # opaque hex, not an id
    # Keyed on the app secret, not a bare hash of the user id. Otherwise
    # anyone holding this table could rebuild the mapping by hashing
    # sequential ids.
    monkeypatch.setattr(analytics.security, "SECRET_KEY", b"a-different-secret")
    assert analytics.visitor_id(user) != handle


def test_a_repeat_visitor_is_new_only_once(db):
    user = make_user(db)
    analytics.record_visit(user)
    analytics.flush(db)
    rows = db.query(models.AnalyticsVisitorDay).all()
    assert len(rows) == 1 and rows[0].is_new

    # Same visitor, a later day: seen before, so not new. The row is not
    # merged into the first day's row either.
    tomorrow = (models.utcnow().date() + timedelta(days=1)).isoformat()
    analytics._visits[(tomorrow, analytics.visitor_id(user))] = set()
    analytics.flush(db)
    rows = db.query(models.AnalyticsVisitorDay).order_by(models.AnalyticsVisitorDay.day).all()
    assert [row.is_new for row in rows] == [True, False]


def test_one_row_per_visitor_per_day_however_much_they_do(db):
    user = make_user(db)
    for _ in range(5):
        analytics.record_event(analytics.EV_ADVENTURE, user)
        analytics.flush(db)
    assert db.query(models.AnalyticsVisitorDay).count() == 1
    assert counter(db, analytics.M_EVENT, analytics.EV_ADVENTURE) == 5


def test_funnel_flags_only_ever_turn_on(db):
    user = make_user(db)
    analytics.record_event(analytics.EV_TURN, user)
    analytics.flush(db)
    # A later visit that reaches no funnel step must not clear the earlier one.
    analytics.record_visit(user)
    analytics.flush(db)
    row = db.query(models.AnalyticsVisitorDay).one()
    assert row.played and not row.created


def test_purge_drops_only_rows_past_the_horizon(db):
    old = (models.utcnow().date() - timedelta(days=analytics.RETENTION_DAYS + 1)).isoformat()
    db.add(models.AnalyticsVisitorDay(day=old, visitor="a" * 32))
    db.add(models.AnalyticsVisitorDay(day=analytics._today(), visitor="b" * 32))
    db.commit()
    assert analytics.purge_old_visitor_days(db) == 1
    assert [r.visitor for r in db.query(models.AnalyticsVisitorDay)] == ["b" * 32]


# ---------- Normalizing what a browser claims ----------

@pytest.mark.parametrize("path, expected", [
    ("/", "/"),
    ("/adventures", "/adventures"),
    ("/adventures/", "/adventures"),
    ("/play/12?x=1", "/play/:id"),
    ("/scenarios/9#top", "/scenarios/:id"),
    ("/wp-admin", "(other)"),
    ("/play/../../etc", "(other)"),
    ("", "/"),
])
def test_route_normalization(path, expected):
    assert analytics.normalize_route(path) == expected


@pytest.mark.parametrize("referrer, expected", [
    ("", "(direct)"),
    ("https://news.ycombinator.com/item?id=1", "news.ycombinator.com"),
    ("https://www.google.com/", "google.com"),
    ("https://ai-dnd.example/scenarios", ""),          # our own host: not a referral
    ("javascript:alert(1)", "(other)"),
    ("https://" + "x" * 200 + ".com", "(other)"),
])
def test_referrer_normalization(referrer, expected):
    assert analytics.normalize_referrer(referrer, "ai-dnd.example") == expected


@pytest.mark.parametrize("ua, expected", [
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit", "mobile"),
    ("Mozilla/5.0 (iPad; CPU OS 17_0) AppleWebKit", "tablet"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "desktop"),
    ("Googlebot/2.1", "bot"),
    ("", "(unknown)"),
])
def test_device_detection(ua, expected):
    assert analytics.device_of(ua) == expected


def test_only_iso_looking_country_headers_are_trusted():
    assert analytics.country_of({"cf-ipcountry": "de"}) == "DE"
    assert analytics.country_of({"cf-ipcountry": "Norway"}) == analytics.UNKNOWN
    assert analytics.country_of({"cf-ipcountry": "XX"}) == analytics.UNKNOWN
    assert analytics.country_of({}) == analytics.UNKNOWN


def test_error_labels_use_the_route_not_the_path():
    class Route:
        path = "/api/adventures/{adventure_id}"

    assert analytics.api_route_label({"route": Route()}, 500) == "500 /api/adventures/{adventure_id}"
    # An unmatched path is entirely attacker-chosen, so it never becomes a label.
    assert analytics.api_route_label({}, 404) == "404 (unmatched)"


# ---------- The summary ----------

def test_summary_counts_people_once_per_step(db):
    one, two = make_user(db), make_user(db)
    for _ in range(3):
        analytics.record_event(analytics.EV_SCENARIO_OPEN, one)
        analytics.record_event(analytics.EV_TURN, one)
    analytics.record_event(analytics.EV_SCENARIO_OPEN, two)

    result = analytics.summary(db, days=7)
    steps = {row["step"]: row["count"] for row in result["funnel"]}
    assert steps["Visited"] == 2
    assert steps["Opened a scenario"] == 2
    assert steps["Played a turn"] == 1      # not 3, because one person made three turns
    assert steps["Signed up"] == 0
    # Raw event totals still count every occurrence.
    assert result["totals"]["turns"] == 3
    assert result["totals"]["visitors"] == 2


def test_summary_series_covers_every_day_including_empty_ones(db):
    analytics.record(analytics.M_PAGE, "/")
    result = analytics.summary(db, days=7)
    assert len(result["series"]) == 7
    assert result["series"][-1]["day"] == models.utcnow().date().isoformat()
    assert result["series"][-1]["pageviews"] == 1
    assert result["series"][0]["pageviews"] == 0


def test_summary_flushes_before_reading(db):
    analytics.record(analytics.M_EVENT, analytics.EV_TURN)
    # Never flushed by hand: the dashboard must not be up to a minute stale.
    assert analytics.summary(db, days=1)["totals"]["turns"] == 1


def test_summary_reports_pages_referrers_and_errors(db):
    analytics.record(analytics.M_PAGE, "/play/:id", n=4)
    analytics.record(analytics.M_REFERRER, "news.ycombinator.com", n=2)
    analytics.record(analytics.M_ERROR, "500 /api/adventures/{adventure_id}")
    result = analytics.summary(db, days=30)
    assert result["pages"][0] == {"label": "/play/:id", "hits": 4}
    assert result["referrers"][0]["label"] == "news.ycombinator.com"
    assert result["totals"]["errors"] == 1


# ---------- The endpoints ----------

@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    visitor = models.User(is_guest=True)
    owner = models.User(is_guest=False, email="owner@example.com")
    setup.add_all([visitor, owner])
    setup.commit()
    ids = {"visitor": visitor.id, "owner": owner.id}
    setup.close()

    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    # Multi-user is what makes the gate mean anything: local mode trusts
    # whoever is at the keyboard, because it is the operator's own machine.
    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(auth, "ANALYTICS_EMAILS", {"owner@example.com"})

    current = {"id": ids["visitor"]}

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, current["id"])

    app.dependency_overrides[auth.get_current_user] = _current_user
    monkeypatch.setattr(
        auth, "resolve_session_user", lambda request, db: db.get(models.User, current["id"])
    )
    try:
        client = TestClient(app)
        client.ids, client.current = ids, current
        yield client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def read_summary(client, days=30):
    return client.get(f"/api/analytics/summary?days={days}")


def test_dashboard_is_invisible_to_everyone_but_the_owner(client):
    assert read_summary(client).status_code == 404
    client.current["id"] = client.ids["owner"]
    assert read_summary(client).status_code == 200


def test_collect_records_a_pageview_and_the_visit(client):
    resp = client.post("/api/analytics/collect", json={"path": "/play/7", "first": True,
                                                       "referrer": "https://news.ycombinator.com/"})
    assert resp.status_code == 204
    client.current["id"] = client.ids["owner"]
    body = read_summary(client).json()
    assert body["pages"][0] == {"label": "/play/:id", "hits": 1}
    assert body["referrers"][0]["label"] == "news.ycombinator.com"
    assert body["totals"]["visitors"] == 1


def test_referrer_and_device_are_recorded_once_per_visit_not_per_view(client):
    for path in ("/", "/scenarios", "/adventures"):
        client.post("/api/analytics/collect", json={"path": path, "first": path == "/"})
    client.current["id"] = client.ids["owner"]
    body = read_summary(client).json()
    assert body["totals"]["pageviews"] == 3
    # Three views, one visit: the referral and the device are facts about the
    # visit, so counting them per view would multiply every one of them.
    assert sum(row["hits"] for row in body["devices"]) == 1


def test_the_owners_own_visits_are_not_traffic(client):
    client.current["id"] = client.ids["owner"]
    client.post("/api/analytics/collect", json={"path": "/", "first": True})
    assert read_summary(client).json()["totals"]["pageviews"] == 0


def test_a_client_cannot_invent_pages_or_events(client):
    client.post("/api/analytics/collect", json={"path": "/../../admin", "first": True})
    # There is no field for it, so a made-up event is not even expressible.
    client.post("/api/analytics/collect", json={"path": "/", "event": "signup"})
    client.current["id"] = client.ids["owner"]
    body = read_summary(client).json()
    assert {row["label"] for row in body["pages"]} == {"(other)", "/"}
    assert body["totals"]["signups"] == 0


def test_api_errors_are_counted_by_route(client):
    client.get("/api/adventures/999999")
    client.current["id"] = client.ids["owner"]
    errors = read_summary(client).json()["errors"]
    assert errors and errors[0]["label"].startswith("404 /api/adventures/")


# ---------- The dialect the tests never run on ----------

def test_the_upserts_compile_for_postgres():
    """Prod runs on Neon, but these tests run on SQLite, and a failed flush
    is caught and logged instead of raised. A dialect mistake would
    therefore stay invisible until the dashboard quietly stayed empty. This
    test compiles both statements against Postgres without connecting to
    one.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=create_engine("postgresql+psycopg://u:p@localhost/db"))()
    compiled = []

    def capture(statement, *args, **kwargs):
        compiled.append(str(statement.compile(dialect=postgresql.dialect())))

    session.execute = capture
    session.scalars = lambda *a, **k: []

    analytics._write_counts(session, {("2026-01-01", "pageview", "/"): 2})
    analytics._write_visits(session, {("2026-01-01", "f" * 32): {"played"}})

    counts, visits = compiled
    assert "ON CONFLICT (day, metric, label) DO UPDATE" in counts
    assert "analytics_daily.hits + excluded.hits" in counts
    assert "ON CONFLICT (day, visitor) DO UPDATE" in visits
    assert "analytics_visitor_days.played OR excluded.played" in visits
    # is_new is settled by the first write of a visitor's first day and must
    # not be in the update clause at all.
    assert "is_new" not in visits.split("DO UPDATE")[1]
