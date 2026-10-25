"""The move from onrender.com to the new domain: the old host's redirect and
the new host's handoff endpoint, which together carry a session across.

    python -m pytest tests/test_domain_handoff.py -v
"""
import importlib
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, security
from app.database import Base, SessionLocal, engine
from app.main import app
from app import models


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "MULTI_USER", True)
    monkeypatch.setattr(auth, "COOKIE_SECURE", False)
    Base.metadata.create_all(engine)
    yield TestClient(app)
    Base.metadata.drop_all(engine)


def _user(is_guest=True, email=None) -> int:
    db = SessionLocal()
    user = models.User(is_guest=is_guest, email=email)
    db.add(user)
    db.commit()
    uid = user.id
    db.close()
    return uid


def test_handoff_token_expires_and_is_not_a_session():
    token = security.sign_handoff(7, now=1000)
    assert security.verify_handoff(token, now=1000 + 60) == 7
    assert security.verify_handoff(token, now=1000 + 121) is None
    assert security.verify_session(token) is None
    assert security.verify_handoff(security.sign_session(7)) is None
    assert security.verify_handoff(token[:-1] + "0") is None


def test_handoff_sets_session_and_keeps_path(client):
    uid = _user()
    r = client.get(
        "/api/auth/handoff",
        params={"t": security.sign_handoff(uid), "next": "/play/3?x=1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/play/3?x=1&from=render"
    assert security.verify_session(r.cookies[auth.SESSION_COOKIE]) == uid


def test_handoff_rejects_offsite_next_and_bad_token(client):
    r = client.get(
        "/api/auth/handoff",
        params={"t": "h1.1.1.bad", "next": "//evil.com/x"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/?from=render"
    assert auth.SESSION_COOKIE not in r.cookies


def test_handoff_keeps_existing_registered_session(client):
    mine = _user(is_guest=False, email="me@example.com")
    other = _user()
    client.cookies.set(auth.SESSION_COOKIE, security.sign_session(mine))
    r = client.get(
        "/api/auth/handoff",
        params={"t": security.sign_handoff(other)},
        follow_redirects=False,
    )
    assert auth.SESSION_COOKIE not in r.cookies


def test_redirect_app_carries_session(monkeypatch):
    monkeypatch.setenv("AIDND_REDIRECT_TO", "https://new.example/")
    from app import redirect
    redirect = importlib.reload(redirect)
    old = TestClient(redirect.app)

    assert old.get("/api/health").status_code == 200
    assert old.post("/api/adventures").status_code == 410

    old.cookies.set(auth.SESSION_COOKIE, security.sign_session(42))
    r = old.get("/play/9?tab=a", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://new.example/api/auth/handoff?")
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(loc).query)
    assert q["next"] == ["/play/9?tab=a"]
    assert security.verify_handoff(q["t"][0], now=time.time()) == 42
