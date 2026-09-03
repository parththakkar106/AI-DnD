"""The access log: app/accesslog.py and GET /api/analytics/access.

This is the half of the analytics work that identifies people on purpose,
so these tests pin the details that would quietly make it wrong. The
address recorded must be the hardened one, not a header a client chose.
Session rows must be thinned instead of written on every page load. And a
row must outlive the account it describes, because guest cleanup deletes
accounts on a schedule, and a log that deletes itself is not a log. The
person page adds a second limit to hold: it reports what someone played,
and never what they wrote.

    python -m pytest tests/test_accesslog.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import accesslog, analytics, auth, limits, models, security
from app.database import Base, SessionLocal, engine, get_db
from app.main import app

EDGE = "198.51.100.77"      # what the trusted proxy appended
SPOOF = "10.0.0.1"          # what a client put in front of it
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 "
          "Safari/604.1")


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


# ---------- One person ----------

def read_person(client, user_id):
    return client.get(f"/api/analytics/access/{user_id}")


def played(user_id, *, turns=0, scenario=None):
    """Gives a user an adventure with `turns` AI replies in it."""
    db = SessionLocal()
    try:
        adventure = models.Adventure(user_id=user_id, title="Their story")
        if scenario is not None:
            adventure.scenario_id = scenario
        db.add(adventure)
        db.flush()
        for _ in range(turns):
            db.add(models.Action(adventure_id=adventure.id, type="ai", text="..."))
        db.commit()
        return adventure.id
    finally:
        db.close()


def scenario(title, *, public, user_id=None):
    db = SessionLocal()
    try:
        row = models.Scenario(title=title, is_public=public, user_id=user_id)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_a_person_page_says_what_they_played(client):
    member = client.ids["member"]
    client.post("/api/auth/login", json={"email": "player@example.com", "password": "hunter2long"},
                headers={"x-forwarded-for": EDGE})
    played(member, turns=3)
    played(member, turns=2)

    detail = read_person(client, member).json()
    assert detail["who"] == "player@example.com"
    assert detail["account_exists"] and not detail["is_guest"]
    assert detail["adventures"] == 2
    # A turn is one AI reply, which is what the dashboard counts as a turn.
    assert detail["turns"] == 5
    assert detail["log"]["logins"] == 1
    assert detail["last_played_at"] and detail["first_adventure_at"]


def test_a_person_page_dates_the_registration_from_the_log(client):
    visit(client)               # creates the guest whose session then registers
    guest = rows()[0].user_id
    client.act_as(guest)
    client.post("/api/auth/register", json={"email": "new@example.com", "password": "hunter2long"})
    client.act_as(client.ids["owner"])

    detail = read_person(client, guest).json()
    # `User.created_at` is when the guest row was made, because registration
    # upgrades that row in place. The register row is the date they signed up.
    assert detail["log"]["registered_at"] and detail["account_since"]
    assert not detail["is_guest"]


def test_a_person_page_names_shared_scenarios_but_not_their_own(client):
    member = client.ids["member"]
    played(member, turns=1, scenario=scenario("The Sunken Library", public=True))
    played(member, turns=1, scenario=scenario("My private draft", public=False, user_id=member))

    detail = read_person(client, member).json()
    # A scenario everyone can see is a choice worth reporting. One they wrote is
    # their writing, so it is counted and left unnamed.
    assert [row["title"] for row in detail["scenarios"]] == ["The Sunken Library"]
    assert detail["own_scenarios"] == 1


def test_a_person_page_lists_the_addresses_they_arrived_from(client):
    visit(client)
    guest = rows()[0].user_id
    visit(client, ip="203.0.113.9", ua=IPHONE)

    detail = read_person(client, guest).json()
    assert [place["ip"] for place in detail["places"]] == ["203.0.113.9", EDGE]
    assert detail["log"]["sessions"] == 2 and detail["is_guest"]


def test_a_person_page_names_the_browsers_they_arrive_in(client):
    visit(client)                                   # the desktop default
    guest = rows()[0].user_id
    visit(client, ip="203.0.113.9", ua=IPHONE)

    devices = read_person(client, guest).json()["devices"]
    # Newest first, and each one says what to test on rather than just "phone".
    assert [(row["device"], row["browser"], row["platform"]) for row in devices] == [
        ("mobile", "Safari 17", "iOS 17"),
        ("desktop", analytics.UNKNOWN, "Windows"),
    ]
    # The string itself is still there, because a parser that does not know a
    # browser should not be the reason the operator cannot see it.
    assert devices[0]["user_agent"] == IPHONE


def test_a_person_page_outlives_the_account(client):
    visit(client)
    entry = rows()[0]
    played(entry.user_id, turns=2)
    db = SessionLocal()
    try:
        db.delete(db.get(models.User, entry.user_id))
        db.commit()
    finally:
        db.close()

    detail = read_person(client, entry.user_id).json()
    # Guest cleanup deletes accounts on a schedule. The rows stay, so the page
    # still names them and reports that the account is gone.
    assert detail["who"] == entry.who and not detail["account_exists"]
    assert detail["log"]["sessions"] == 1
    # The adventures went with the account, which is what the cascade is for.
    assert detail["adventures"] == 0


def test_a_failed_sign_in_has_no_person_page(client):
    client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "wrong"},
                headers={"x-forwarded-for": EDGE})
    failure = rows(accesslog.LOGIN_FAILED)[0]
    # The row names an address, not an account, so there is nothing to open.
    assert failure.user_id is None
    assert read_person(client, 9999).status_code == 404


def test_the_person_page_is_owner_only(client):
    visit(client)
    guest = rows()[0].user_id
    assert read_person(client, guest).status_code == 200
    client.act_as(client.ids["member"])
    assert read_person(client, guest).status_code == 404


# ---------- What to test on ----------

def read_devices(client):
    return client.get("/api/analytics/access/devices")


def test_the_device_list_groups_people_by_what_they_browse_in(client):
    visit(client)                                   # one guest, desktop
    visit(client, ip="203.0.113.9", ua=IPHONE)      # the same guest, on a phone
    client.post("/api/auth/login", json={"email": "player@example.com", "password": "hunter2long"},
                headers={"x-forwarded-for": EDGE, "user-agent": IPHONE})

    rows_by_key = {
        (row["browser"], row["platform"]): row
        for row in read_devices(client).json()["devices"]
    }
    phone = rows_by_key[("Safari 17", "iOS 17")]
    # Two people arrived on that browser, one of them twice; the row counts each
    # person once and every arrival separately.
    assert phone["people"] == 2 and phone["hits"] == 2 and phone["device"] == "mobile"
    assert rows_by_key[(analytics.UNKNOWN, "Windows")]["people"] == 1


def test_the_device_list_counts_a_failed_attempt_as_nobody(client):
    client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "wrong"},
                headers={"x-forwarded-for": EDGE, "user-agent": IPHONE})

    row = read_devices(client).json()["devices"][0]
    # The browser reached the site, so it is an arrival. It answers for no
    # account, so it is nobody.
    assert row["hits"] == 1 and row["people"] == 0


def test_the_device_list_is_owner_only(client):
    visit(client)
    assert read_devices(client).status_code == 200
    client.act_as(client.ids["member"])
    # "devices" must not be read as a user id by the person page, whichever
    # gate answers first.
    assert read_devices(client).status_code == 404
