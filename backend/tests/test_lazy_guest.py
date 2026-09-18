"""Arriving is not an account: app/guests.py and the endpoints that allow it.

A visitor used to get a `users` row and a copy of the starter adventure the
moment they loaded the page, because `GET /api/auth/me` created one for anybody
who arrived without a cookie. Three of the SPA's first-paint calls could each
arrive cookie-less, so one cold load could write three of them.

What holds now:

* Loading the app, listing your adventures, browsing the shared scenarios and
  reading the settings write nothing at all, for anybody.
* The account appears the first time the visitor does something that needs one,
  and their cookie changes from a visitor token to a session token in the same
  response.
* One visitor gets one account however many of their requests arrive together.
  The lookup catches the ordinary case; the unique index on `visitor_key`
  catches the race, which is the failure this whole change exists to remove.

    python -m pytest tests/test_lazy_guest.py -v
"""
import pytest
from fastapi.testclient import TestClient

from app import accesslog, auth, guests, limits, models, security
from app.database import Base, SessionLocal, engine
from app.main import app

EDGE = "198.51.100.77"


@pytest.fixture(autouse=True)
def clean_state():
    accesslog._last_session.clear()
    yield
    accesslog._last_session.clear()


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)
    # No dependency override here, unlike most of the suite: the point of these
    # tests is which requests resolve a caller into an account, so the real
    # resolution has to run.
    try:
        yield TestClient(app)
    finally:
        Base.metadata.drop_all(bind=engine)


def users() -> list[models.User]:
    db = SessionLocal()
    try:
        return db.query(models.User).all()
    finally:
        db.close()


def adventures() -> int:
    db = SessionLocal()
    try:
        return db.query(models.Adventure).count()
    finally:
        db.close()


def log(kind=None) -> list[models.AccessEvent]:
    db = SessionLocal()
    try:
        query = db.query(models.AccessEvent).order_by(models.AccessEvent.id)
        return query.filter_by(kind=kind).all() if kind else query.all()
    finally:
        db.close()


def public_scenario(title="The Sunken Library") -> int:
    db = SessionLocal()
    try:
        row = models.Scenario(title=title, is_public=True, user_id=None)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# ---------- Arriving ----------

def test_a_cold_first_paint_writes_nobody(client):
    """The three calls the SPA makes on load, in the order it makes them."""
    title = "The Sunken Library"
    public_scenario(title)
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/adventures").json() == []
    # The seeded demos are here too, so this names the one it made rather than
    # counting rows.
    assert title in [row["title"] for row in client.get("/api/scenarios").json()]

    assert users() == []
    assert adventures() == 0


def test_the_visitor_is_told_what_they_are(client):
    body = client.get("/api/auth/me").json()
    # `is_guest` is what the interface plays as, and `visitor` is what says no
    # row exists behind it yet.
    assert body["visitor"] and body["is_guest"]
    assert body["id"] is None and body["email"] is None


def test_the_visitor_cookie_comes_back_and_is_reused(client):
    first = client.get("/api/auth/me")
    token = first.cookies[auth.SESSION_COOKIE]
    assert security.verify_visitor(token)
    # The second call recognizes the browser rather than minting another name,
    # which is what keeps the access log and the visit counters honest.
    client.get("/api/auth/me")
    assert client.cookies[auth.SESSION_COOKIE] == token


def test_a_visitor_token_cannot_stand_in_for_an_account(client):
    token = client.get("/api/auth/me").cookies[auth.SESSION_COOKIE]
    assert security.verify_session(token) is None
    assert security.verify_visitor(security.sign_session(1)) is None


def test_an_unsigned_visitor_cookie_is_not_a_visitor(client):
    """Without the signature a client could invent a visitor per request."""
    client.cookies.set(auth.SESSION_COOKIE, "n1.made-up-id.not-a-signature")
    assert client.post("/api/adventures", json={}).status_code == 401
    assert users() == []


def test_a_caller_with_no_cookie_at_all_is_asked_to_bootstrap(client):
    assert client.post("/api/adventures", json={}).status_code == 401
    assert users() == []


def test_settings_are_the_defaults_and_no_row_is_written(client):
    client.get("/api/auth/me")
    body = client.get("/api/settings").json()
    assert body["model"] == "" and not body["has_api_key"]
    assert body["memory_top_k"] == models.Settings.__table__.c.memory_top_k.default.arg
    db = SessionLocal()
    try:
        assert db.query(models.Settings).count() == 0
    finally:
        db.close()


def test_a_visitor_sees_the_shared_scenarios_and_nobody_elses(client):
    public_scenario()
    db = SessionLocal()
    try:
        someone = models.User(is_guest=True)
        db.add(someone)
        db.commit()
        db.add(models.Scenario(title="Their draft", is_public=False, user_id=someone.id))
        db.commit()
        private_id = db.query(models.Scenario).filter_by(is_public=False).one().id
    finally:
        db.close()

    client.get("/api/auth/me")
    visible = [row["title"] for row in client.get("/api/scenarios").json()]
    assert "The Sunken Library" in visible and "Their draft" not in visible
    assert client.get(f"/api/scenarios/{private_id}").status_code == 404


# ---------- Becoming a guest ----------

def test_starting_an_adventure_writes_the_account(client):
    scenario_id = public_scenario()
    client.get("/api/auth/me")
    assert users() == []

    created = client.post("/api/adventures", json={"scenario_id": scenario_id})
    assert created.status_code == 201

    written = users()
    assert len(written) == 1
    guest = written[0]
    assert guest.is_guest and guest.email is None
    # The account remembers the visitor it was written down for. That is what
    # the unique index is on, and what carries their handle in the counters.
    assert guest.visitor_key


def test_the_cookie_becomes_a_session_cookie_in_the_same_response(client):
    scenario_id = public_scenario()
    client.get("/api/auth/me")
    created = client.post("/api/adventures", json={"scenario_id": scenario_id})

    token = created.cookies[auth.SESSION_COOKIE]
    assert security.verify_session(token) == users()[0].id
    # And the app agrees on the next call, with no second bootstrap.
    body = client.get("/api/auth/me").json()
    assert not body["visitor"] and body["is_guest"] and body["id"] == users()[0].id


def test_the_starter_adventure_comes_with_the_account(client):
    client.get("/api/auth/me")
    client.post("/api/adventures", json={})
    titles = [row["title"] for row in client.get("/api/adventures").json()]
    # Their own new one, and the pre-played copy every guest is given.
    assert len(titles) == 2
    assert any("Pokemon" in title for title in titles)


def test_registering_from_a_visit_is_one_account(client):
    client.get("/api/auth/me")
    registered = client.post(
        "/api/auth/register",
        json={"email": "new@example.com", "password": "hunter2long"},
    )
    assert registered.status_code == 200
    written = users()
    assert len(written) == 1
    assert written[0].email == "new@example.com" and not written[0].is_guest


def test_the_log_records_the_arrival_and_then_the_upgrade(client):
    client.get("/api/auth/me", headers={"x-forwarded-for": EDGE})
    client.post("/api/adventures", json={})

    arrival = log(accesslog.SESSION)[0]
    assert arrival.who.startswith("Visitor #") and arrival.user_id is None

    upgrade = log(accesslog.ADOPTED)[0]
    guest = users()[0]
    # The row that ties the two names together, which is the only place the
    # visitor's name and their account's name appear on the same line.
    assert upgrade.user_id == guest.id
    assert upgrade.who.startswith(f"Guest #{guest.id} (was Visitor #")
    assert upgrade.is_guest


# ---------- One visitor, one account ----------

def test_a_second_request_from_the_same_visitor_reuses_the_account(client):
    client.get("/api/auth/me")
    client.post("/api/adventures", json={})
    client.post("/api/adventures", json={})
    assert len(users()) == 1


def test_two_of_a_visitors_requests_cannot_make_two_accounts(client, monkeypatch):
    """The race the lookup alone cannot close.

    Two requests from one browser can both look, both find nothing, and both
    insert. `find` is stubbed to miss once, which is that window held open. The
    unique index is what decides it, and the loser reads back the winner rather
    than failing the request it was serving.
    """
    visitor = auth.new_visitor()
    request = _request_stub()
    response = _response_stub()

    first = guests.adopt(SessionLocal(), visitor, request, response)

    real_find = guests.find
    looked = {"n": 0}

    def find_nothing_once(db, who):
        looked["n"] += 1
        return None if looked["n"] == 1 else real_find(db, who)

    monkeypatch.setattr(guests, "find", find_nothing_once)
    second = guests.adopt(SessionLocal(), visitor, request, response)

    assert second.id == first.id
    assert len(users()) == 1


def _request_stub():
    class Request:
        headers = {"user-agent": "pytest"}
        cookies: dict = {}
        client = None
    return Request()


def _response_stub():
    class Response:
        def set_cookie(self, *args, **kwargs):
            pass
    return Response()
