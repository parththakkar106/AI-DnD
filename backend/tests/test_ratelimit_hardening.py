"""Regression tests for the X-Forwarded-For rate-limit bypass and the
per-account login throttle added to close it.

Background: uvicorn's `--forwarded-allow-ips "*"` trusted the leftmost
`X-Forwarded-For` entry. The client controls that entry, so rotating the
header issued a fresh rate-limit bucket on every request. `client_ip` now
reads the hop the trusted edge appends, which is the rightmost one. Login
also has an email-keyed throttle that no IP trick can weaken.

    python -m pytest tests/test_ratelimit_hardening.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest

from app import auth, limits


class _Req:
    """Minimal stand-in for starlette's Request: a header lookup and a peer."""

    def __init__(self, xff: str | None, peer: str | None = "10.0.0.1"):
        self.headers = {} if xff is None else {"x-forwarded-for": xff}
        self.client = None if peer is None else type("C", (), {"host": peer})()


# ---------- client_ip: the spoof-resistant hop ----------

def test_client_ip_takes_appended_rightmost_hop(monkeypatch):
    monkeypatch.setattr(limits, "TRUSTED_PROXY_HOPS", 1)
    # An attacker prepends a fake IP. The edge appends the real one on the right.
    req = _Req("203.0.113.9, 198.51.100.77")
    assert limits.client_ip(req) == "198.51.100.77"


def test_client_ip_ignores_spoofed_leftmost(monkeypatch):
    monkeypatch.setattr(limits, "TRUSTED_PROXY_HOPS", 1)
    # The keyed IP stays the real hop regardless of what the client adds on
    # the left, so rotating that value no longer creates a new bucket.
    a = limits.client_ip(_Req("1.1.1.1, 198.51.100.77"))
    b = limits.client_ip(_Req("2.2.2.2, 198.51.100.77"))
    c = limits.client_ip(_Req("evil, junk, 198.51.100.77"))
    assert a == b == c == "198.51.100.77"


def test_client_ip_honours_extra_trusted_hops(monkeypatch):
    monkeypatch.setattr(limits, "TRUSTED_PROXY_HOPS", 2)
    # Two trusted hops: real client is second from the right.
    req = _Req("9.9.9.9, 203.0.113.5, 198.51.100.77")
    assert limits.client_ip(req) == "203.0.113.5"


def test_client_ip_falls_back_to_socket_peer():
    assert limits.client_ip(_Req(None, peer="172.16.0.4")) == "172.16.0.4"
    assert limits.client_ip(_Req(None, peer=None)) == "unknown"


# ---------- per-account login throttle ----------

@pytest.fixture(autouse=True)
def _multi_user(monkeypatch):
    monkeypatch.setattr(auth, "MULTI_USER", True)
    # Isolate the module-level failure map for each test.
    from collections import defaultdict, deque
    monkeypatch.setattr(limits, "_login_fails", defaultdict(deque))


def test_login_throttle_blocks_after_limit():
    email = "victim@example.com"
    # Each attempt up to the limit is allowed and recorded as a failure.
    for _ in range(limits.LOGIN_FAIL_LIMIT):
        limits.check_login_allowed(email)      # does not raise
        limits.note_login_failure(email)
    # One more failure exceeds the limit.
    with pytest.raises(limits.HTTPException) as exc:
        limits.check_login_allowed(email)
    assert exc.value.status_code == 429


def test_login_throttle_is_per_account():
    for _ in range(limits.LOGIN_FAIL_LIMIT):
        limits.note_login_failure("a@example.com")
    with pytest.raises(limits.HTTPException):
        limits.check_login_allowed("a@example.com")
    # A different account is unaffected because the throttle keys on email, not IP.
    limits.check_login_allowed("b@example.com")  # must not raise


def test_successful_login_clears_the_streak():
    email = "typo@example.com"
    for _ in range(limits.LOGIN_FAIL_LIMIT):
        limits.note_login_failure(email)
    limits.note_login_success(email)
    limits.check_login_allowed(email)  # failure streak cleared, must not raise


def test_throttle_is_noop_in_local_mode(monkeypatch):
    monkeypatch.setattr(auth, "MULTI_USER", False)
    for _ in range(limits.LOGIN_FAIL_LIMIT * 3):
        limits.note_login_failure("solo@example.com")
    limits.check_login_allowed("solo@example.com")  # never throttled locally
