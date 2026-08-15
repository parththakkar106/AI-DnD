"""Tests for the SSRF guard on the user-supplied BYOK endpoint_url.

    python -m pytest tests/test_netguard.py -v
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AIDND_DB_PATH"] = _tmp.name
os.environ.pop("AIDND_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)

import pytest

from app import auth, netguard


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(auth, "MULTI_USER", True)


def _resolves_to(monkeypatch, ip: str):
    """Pin getaddrinfo so we test the address decision, not real DNS."""
    monkeypatch.setattr(
        netguard.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (ip, 443))],
    )


@pytest.mark.parametrize("ip", [
    "127.0.0.1",          # loopback
    "169.254.169.254",    # cloud metadata (link-local)
    "10.0.0.5",           # RFC1918
    "192.168.1.1",        # RFC1918
    "172.16.0.9",         # RFC1918
    "0.0.0.0",            # unspecified
    "100.64.0.1",         # carrier-grade NAT
    "::1",                # IPv6 loopback
    "fd00::1",            # IPv6 unique-local
])
def test_blocks_non_public_addresses(hosted, monkeypatch, ip):
    _resolves_to(monkeypatch, ip)
    assert netguard.endpoint_block_reason("https://evil.example.com/v1") is not None


def test_allows_public_address(hosted, monkeypatch):
    _resolves_to(monkeypatch, "104.18.0.1")  # a public IP
    assert netguard.endpoint_block_reason("https://openrouter.ai/api/v1") is None


def test_rejects_non_http_scheme(hosted):
    assert netguard.endpoint_block_reason("file:///etc/passwd") is not None
    assert netguard.endpoint_block_reason("gopher://x/") is not None


def test_unresolvable_host_is_blocked(hosted, monkeypatch):
    def boom(*a, **k):
        raise netguard.socket.gaierror("no such host")
    monkeypatch.setattr(netguard.socket, "getaddrinfo", boom)
    assert netguard.endpoint_block_reason("https://nope.invalid/v1") is not None


def test_noop_in_local_mode(monkeypatch):
    monkeypatch.setattr(auth, "MULTI_USER", False)
    # Local installs legitimately reach localhost (Ollama) — never blocked.
    assert netguard.endpoint_block_reason("http://localhost:11434/v1") is None
    assert netguard.endpoint_block_reason("http://127.0.0.1:11434/v1") is None
