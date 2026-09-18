"""Phase 8: secrets and crypto primitives for optional accounts.

Everything derives from one server-side secret:

* Session cookies are HMAC-signed with it.
* Stored LLM API keys are Fernet-encrypted with a key derived from it.

The secret comes from `AIDND_SECRET_KEY`, or it is generated once into
`secret.key` next to the database, so a local install and a Docker volume work
with no configuration. Losing that file logs everyone out and makes the stored
API keys unreadable, and users then re-enter them. A multi-user deployment has
to set the environment variable, because a hosted filesystem is ephemeral and a
`secret.key` regenerated on every deploy would log out every user each time.

Passwords use `hashlib.scrypt`, which is in the standard library and backed by
OpenSSL, so this needs no separate hashing dependency.
"""

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .database import DB_PATH

_SECRET_FILE = DB_PATH.parent / "secret.key"


def _load_secret() -> bytes:
    env = os.environ.get("AIDND_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    # Same flag parse as auth.MULTI_USER (auth imports this module, so it
    # can't be imported from there).
    if os.environ.get("AIDND_MULTI_USER", "").strip().lower() in ("1", "true", "yes", "on"):
        raise RuntimeError(
            "AIDND_SECRET_KEY must be set when AIDND_MULTI_USER is on: an "
            "auto-generated secret.key on an ephemeral hosted filesystem would "
            "rotate on every deploy, logging out every user and orphaning "
            "their stored API keys. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_bytes().strip()
    secret = secrets.token_urlsafe(48).encode()
    _SECRET_FILE.write_bytes(secret)
    return secret


SECRET_KEY = _load_secret()
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY).digest()))


# ---------- Password hashing (scrypt) ----------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(key, bytes.fromhex(key_hex))
    except (ValueError, AttributeError):
        return False


# ---------- Session tokens ----------
# Two token shapes share the session cookie, and both are "<version>.<id>.<hmac>".
# Neither expires, because a long-lived guest session is what this is for.
#
# "v1.<user_id>" names an account: a guest, a registered user, or the local
# user. "n1.<visitor_id>" names a browser that has been seen and not yet written
# down, which has no row anywhere and whose id is random rather than a primary
# key. A visitor who starts playing trades their token for a v1 one, and
# `verify_session` and `verify_visitor` each reject the other's shape, so a
# caller cannot present a visitor token where an account is required.

VISITOR_VERSION = "n1"


def new_visitor_id() -> str:
    """Returns the random id that names one browser we have not written down.

    It carries no meaning and indexes nothing. It exists so that repeat requests
    from one browser can be recognized as one visitor before there is an account
    to recognize them by.
    """
    return secrets.token_urlsafe(12)


def sign_visitor(visitor_id: str) -> str:
    payload = f"{VISITOR_VERSION}.{visitor_id}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_visitor(token: str) -> str | None:
    """Returns the visitor id in a visitor token, or None.

    A token naming an account returns None here, the same way a visitor token
    returns None from `verify_session`. The signature is what makes the id
    trustworthy: without it a client could invent a visitor id per request and
    spend the visitor half of the access log.
    """
    try:
        version, visitor_id, sig = token.split(".")
        if version != VISITOR_VERSION or not visitor_id:
            return None
        payload = f"{version}.{visitor_id}"
        expected = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return visitor_id
    except (ValueError, AttributeError):
        return None


def sign_session(user_id: int) -> str:
    payload = f"v1.{user_id}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(token: str) -> int | None:
    try:
        version, user_id, sig = token.split(".")
        if version != "v1":
            return None
        payload = f"{version}.{user_id}"
        expected = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return int(user_id)
    except (ValueError, AttributeError):
        return None


# ---------- API-key encryption at rest ----------
# Stored values carry an "enc:" prefix so plaintext keys from pre-Phase-8
# databases can be recognized and migrated.

ENC_PREFIX = "enc:"


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return ENC_PREFIX + _fernet.encrypt(plain.encode()).decode()


def decrypt_secret(stored: str) -> str:
    """Returns the plaintext key. Tolerates legacy plaintext values (returned
    as-is) and undecryptable tokens (secret rotated → treated as unset)."""
    if not stored:
        return ""
    if not stored.startswith(ENC_PREFIX):
        return stored
    try:
        return _fernet.decrypt(stored[len(ENC_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        return ""
