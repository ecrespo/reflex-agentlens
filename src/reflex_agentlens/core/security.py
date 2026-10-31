"""API keys, secret hashing and HMAC tokens (API §3.4, Tech Design DD-014, REQ-320, REQ-321).

Key format: ``al_<key_id>_<secret>``
- ``key_id``: 8 chars of lowercase Crockford base32 (40 bits), public lookup handle.
- ``secret``: 32 chars of base62 from ``secrets`` (CSPRNG), ≈ 190.5 bits [REQ-320.b].

At rest only ``sha256(salt ‖ secret)`` with a 16-byte random salt is stored, compared with
``hmac.compare_digest`` [REQ-320.a]. Error messages never contain the key or any part of it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import string
from dataclasses import dataclass

from reflex_agentlens.core.errors import ValidationError

__all__ = [
    "API_KEY_PREFIX",
    "SALT_BYTES",
    "SECRET_ENTROPY_BITS",
    "GeneratedKey",
    "SecretHash",
    "generate_api_key",
    "hash_secret",
    "parse_api_key",
    "sign",
    "unsign",
    "verify_secret",
]

API_KEY_PREFIX = "al_"
KEY_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
BASE62_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
KEY_ID_LEN = 8
SECRET_LEN = 32
SECRET_ENTROPY_BITS = SECRET_LEN * math.log2(len(BASE62_ALPHABET))
SALT_BYTES = 16
_KEY_RE = re.compile(r"al_([0-9a-hjkmnp-tv-z]{8})_([0-9A-Za-z]{32})")


@dataclass(frozen=True, slots=True, repr=False)
class GeneratedKey:
    """A freshly generated key. ``plaintext`` is shown to the user once and never stored."""

    key_id: str
    secret: str

    @property
    def plaintext(self) -> str:
        return f"{API_KEY_PREFIX}{self.key_id}_{self.secret}"

    def __repr__(self) -> str:  # never leak the secret through repr/logging
        return f"GeneratedKey(key_id={self.key_id!r}, secret=***)"


@dataclass(frozen=True, slots=True)
class SecretHash:
    salt: bytes
    digest: bytes


def generate_api_key() -> GeneratedKey:
    key_id = "".join(secrets.choice(KEY_ID_ALPHABET) for _ in range(KEY_ID_LEN))
    secret = "".join(secrets.choice(BASE62_ALPHABET) for _ in range(SECRET_LEN))
    return GeneratedKey(key_id, secret)


def parse_api_key(text: str) -> tuple[str, str]:
    """Split ``al_<key_id>_<secret>`` into (key_id, secret); malformed input → ValidationError."""
    m = _KEY_RE.fullmatch(text)
    if not m:
        raise ValidationError("api_key", "malformed")
    return m.group(1), m.group(2)


def _digest(salt: bytes, secret: str) -> bytes:
    return hashlib.sha256(salt + secret.encode()).digest()


def hash_secret(secret: str, salt: bytes | None = None) -> SecretHash:
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    if len(salt) != SALT_BYTES:
        raise ValidationError("salt", f"expected {SALT_BYTES} bytes")
    return SecretHash(salt, _digest(salt, secret))


def verify_secret(secret: str, stored: SecretHash) -> bool:
    """Constant-time comparison of ``sha256(salt ‖ secret)`` with the stored digest."""
    return hmac.compare_digest(_digest(stored.salt, secret), stored.digest)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, purpose: str, payload: bytes) -> bytes:
    return hmac.new(key, purpose.encode() + b"\x00" + payload, hashlib.sha256).digest()


def sign(key: bytes, purpose: str, payload: bytes) -> str:
    """``<b64url(payload)>.<b64url(hmac)>``. ``purpose`` separates cookies from cursors."""
    if len(key) < 32:
        raise ValidationError("signing_key", "must be at least 32 bytes")
    return f"{_b64(payload)}.{_b64(_mac(key, purpose, payload))}"


def unsign(key: bytes, purpose: str, token: str) -> bytes:
    """Return the payload of a token made by :func:`sign`; tampering → ValidationError."""
    try:
        body, mac = token.split(".")
        payload, given = _unb64(body), _unb64(mac)
    except ValueError:
        raise ValidationError("token", "malformed") from None
    if not hmac.compare_digest(_mac(key, purpose, payload), given):
        raise ValidationError("token", "bad signature")
    return payload
