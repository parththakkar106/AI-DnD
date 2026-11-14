"""The feedback endpoints: the daily cap and the owner gate.

    python -m pytest tests/test_feedback.py -v
"""
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, models
from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers.feedback import DAILY_LIMIT


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    owner = models.User(is_guest=False, email="owner@example.com")
    guest = models.User(is_guest=True)
    setup.add_all([owner, guest])
    setup.commit()
    ids = {"owner": owner.id, "guest": guest.id}
    setup.close()

    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(auth, "ANALYTICS_EMAILS", {"owner@example.com"})
    acting = {"id": ids["guest"]}
    app.dependency_overrides[auth.get_current_user] = (
        lambda db=Depends(get_db): db.get(models.User, acting["id"])
    )
    try:
        test_client = TestClient(app)
        test_client.ids = ids
        test_client.act_as = lambda user_id: acting.update(id=user_id)
        yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def test_cap_then_owner_reads_with_user_id(client):
    for n in range(DAILY_LIMIT):
        sent = client.post("/api/feedback", json={"message": f"note {n}"})
        assert sent.status_code == 201
    assert client.post("/api/feedback", json={"message": "one more"}).status_code == 429
    assert client.post("/api/feedback", json={"message": "  "}).status_code == 422
    bad = client.post("/api/feedback", json={"message": "hi", "email": "nope"})
    assert bad.status_code == 422

    # Players cannot read the list.
    assert client.get("/api/feedback").status_code == 404

    client.act_as(client.ids["owner"])
    client.post("/api/feedback", json={"message": "reply me", "email": "a@b.co"})
    items = client.get("/api/feedback").json()["items"]
    assert len(items) == DAILY_LIMIT + 1
    assert items[0]["email"] == "a@b.co"
    assert items[-1]["user_id"] == client.ids["guest"]
    assert items[-1]["is_guest"] is True
