"""The access log: app/accesslog.py and GET /api/analytics/access.

This is the half of the analytics work that identifies people on purpose,
so these tests pin the details that would quietly make it wrong. The
address recorded must be the hardened one, not a header a client chose.
Session rows must be thinned instead of written on every page load. And a
row must outlive the account it describes, because guest cleanup deletes
accounts on a schedule, and a log that deletes itself is not a log.

    python -m pytest tests/test_accesslog.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import accesslog, auth, limits, models, security
from app.database import Base, SessionLocal, engine, get_db
from app.main import app

EDGE = "198.51.100.77"      # what the trusted proxy appended
SPOOF = "10.0.0.1"          # what a client put in front of it


@pytest.fixture(autouse=True)
def clean_state():
    accesslog._last_session.clear()
    yield
    accesslog._last_session.clear()


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    owner = models.User(is_guest=False, email="owner@example.com")
    member = models.User(
        is_guest=False, email="player@example.com",
        password_hash=security.hash_password("hunter2long"),
    )
    setup.add_all([owner, member])
    setup.commit()
    ids = {"owner": owner.id, "member": member.id}
    setup.close()

    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_login_allowed", lambda *a, **k: None)
    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(auth, "ANALYTICS_EMAILS", {"owner@example.com"})

    # /auth/me resolves its own session, so the cookie flow below is the real
    # one. Every other endpoint goes through get_current_user, and `act_as`
    # decides who that is.
    acting = {"id": ids["owner"]}

    def _current(db=Depends(get_db)):
        return db.get(models.User, acting["id"])

    app.dependency_overrides[auth.get_current_user] = _current
    try:
        test_client = TestClient(app)
        test_client.ids = ids
        test_client.act_as = lambda user_id: acting.update(id=user_id)
        yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def visit(client, ip=EDGE, ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"):
    return client.get(
        "/api/auth/me",
        headers={"x-forwarded-for": f"{SPOOF}, {ip}", "user-agent": ua},
    )


def rows(kind=None):
    db = SessionLocal()
    try:
        query = db.query(models.AccessEvent).order_by(models.AccessEvent.id)
        if kind:
            query = query.filter_by(kind=kind)
        return query.all()
    finally:
        db.close()


def read_log(client, **params):
    return client.get("/api/analytics/access", params=params)


# ---------- Writing ----------

def test_a_new_session_is_logged(client):
    visit(client)
    logged = rows()
    assert len(logged) == 1
    entry = logged[0]
    assert entry.kind == accesslog.SESSION
    assert entry.is_guest and entry.who.startswith("Guest #")
    assert entry.device == "desktop"


def test_the_address_is_the_hardened_one_not_the_clients(client):
    visit(client)
    # The client prepended its own value. Only the hop the edge appended counts.
    # Recording the leftmost value would make every row forgeable, which is
    # worse for a log than having no log at all.
    assert rows()[0].ip == EDGE


def test_session_rows_are_thinned_to_one_per_day_per_address(client):
    for _ in range(4):
        visit(client)
    assert len(rows(accesslog.SESSION)) == 1


def test_a_changed_address_writes_a_new_row(client):
    visit(client)
    visit(client, ip="203.0.113.9")
    logged = rows(accesslog.SESSION)
    assert [entry.ip for entry in logged] == [EDGE, "203.0.113.9"]
    # Same session throughout, so both rows name the same visitor.
    assert logged[0].who == logged[1].who


def test_sign_in_and_failure_are_both_logged(client):
    client.post("/api/auth/login", json={"email": "player@example.com", "password": "wrong"},
                headers={"x-forwarded-for": EDGE})
    client.post("/api/auth/login", json={"email": "player@example.com", "password": "hunter2long"},
                headers={"x-forwarded-for": EDGE})
    kinds = [entry.kind for entry in rows()]
    assert accesslog.LOGIN_FAILED in kinds and accesslog.LOGIN in kinds

    failure = rows(accesslog.LOGIN_FAILED)[0]
    # This records the address that was tried, not the account it belongs to.
    # A failed attempt against an address with no matching account is
    # exactly what this row exists to capture.
    assert failure.who == "player@example.com"
    assert failure.user_id is None
    assert rows(accesslog.LOGIN)[0].user_id == client.ids["member"]


def test_registering_is_logged_against_the_upgraded_account(client):
    visit(client)  # creates the guest whose session then registers
    client.act_as(rows()[0].user_id)
    client.post("/api/auth/register", json={"email": "new@example.com", "password": "hunter2long"})
    entry = rows(accesslog.REGISTER)[0]
    assert entry.who == "new@example.com" and not entry.is_guest


def test_a_row_outlives_the_account_it_describes(client):
    visit(client)
    entry = rows()[0]
    db = SessionLocal()
    try:
        db.delete(db.get(models.User, entry.user_id))
        db.commit()
    finally:
        db.close()
    # There is no foreign key, and `who` is a snapshot. Guest cleanup deletes
    # accounts on a schedule, and a log that vanishes along with them is not
    # a log.
    survivor = rows()[0]
    assert survivor.who == entry.who and survivor.ip == EDGE


def test_a_long_user_agent_is_truncated(client):
    visit(client, ua="Mozilla/" + "x" * 500)
    assert len(rows()[0].user_agent) == accesslog.MAX_UA


def test_a_logging_failure_does_not_break_the_request(client, monkeypatch):
    monkeypatch.setattr(accesslog, "_client_ip", lambda request: 1 / 0)
    # The log observes sign-in. A logging failure must not block the request.
    assert visit(client).status_code == 200


# ---------- Reading ----------

def test_the_log_is_invisible_to_everyone_but_the_owner(client):
    visit(client)
    assert read_log(client).status_code == 200
    client.act_as(client.ids["member"])
    assert read_log(client).status_code == 404


def test_the_log_reads_newest_first_and_pages_backwards(client):
    for index in range(5):
        visit(client, ip=f"203.0.113.{index}")
    first = read_log(client, limit=2).json()
    assert [event["ip"] for event in first["events"]] == ["203.0.113.4", "203.0.113.3"]
    assert first["has_more"]

    older = read_log(client, limit=2, before_id=first["events"][-1]["id"]).json()
    assert [event["ip"] for event in older["events"]] == ["203.0.113.2", "203.0.113.1"]


def test_the_log_filters_by_kind_and_searches(client):
    visit(client)
    client.post("/api/auth/login", json={"email": "player@example.com", "password": "hunter2long"},
                headers={"x-forwarded-for": "203.0.113.44"})

    assert len(read_log(client, kind="login").json()["events"]) == 1
    by_email = read_log(client, q="player@example.com").json()["events"]
    assert len(by_email) == 1 and by_email[0]["kind"] == "login"
    by_ip = read_log(client, q="203.0.113.44").json()["events"]
    assert len(by_ip) == 1
    assert read_log(client, q="nobody@example.com").json()["events"] == []
