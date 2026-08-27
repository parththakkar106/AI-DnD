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
# The token is "v1.<user_id>.<hmac>". It does not expire, because a long-lived
# guest session is what this is for.

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
