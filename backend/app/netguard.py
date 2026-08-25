"""SSRF guard for the one place the server makes an outbound request to a
user-supplied address: the BYOK `endpoint_url` (connection test + turns/chat).

Without this guard, a hosted user could point `endpoint_url` at an internal
service or at the cloud metadata endpoint, 169.254.169.254, and have the server
fetch it. The connection test even returns part of the response. The guard
therefore refuses any URL that resolves to a non-public address.

The guard does nothing in local mode. A local install talking to
http://localhost:11434, which is Ollama, is the intended case. The guard applies
only to a hosted, multi-user deployment, where the endpoint comes from an
untrusted visitor.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from . import auth


def endpoint_block_reason(url: str) -> str | None:
    """A human-readable reason this URL must NOT be fetched server-side, or None
    if it's allowed. Resolves the host and rejects it if any resulting address
    is non-public (private, loopback, link-local/metadata, reserved, …).

    Checking at request time (not just on save) is deliberate: it resists a DNS
    record that flips to a private IP after the value was stored.
    """
    if not auth.MULTI_USER:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "the endpoint URL must start with http:// or https://"
    host = parsed.hostname
    if not host:
        return "the endpoint URL has no host"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "the endpoint host could not be resolved"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return "the endpoint host resolved to an unrecognized address"
        # is_global is the strict allowlist: private/loopback/link-local/CGNAT
        # all report False, so this one check covers the metadata IP too.
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            return "the endpoint URL resolves to a non-public address"
    return None
