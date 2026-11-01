"""core.security: API keys, salted hashes, HMAC tokens (plan task P1-06.3, DD-014, API §3.4)."""

from __future__ import annotations

import re
import secrets as std_secrets

import pytest

from reflex_agentlens.core import security
from reflex_agentlens.core.errors import ValidationError

KEY = std_secrets.token_bytes(32)


def test_req_320b_key_format_and_prefix() -> None:
    key = security.generate_api_key()
    assert re.fullmatch(r"al_[0-9a-hjkmnp-tv-z]{8}_[0-9A-Za-z]{32}", key.plaintext)
    assert security.parse_api_key(key.plaintext) == (key.key_id, key.secret)


def test_req_320b_secret_has_at_least_128_bits_of_entropy() -> None:
    assert security.SECRET_ENTROPY_BITS >= 128
    assert len(security.generate_api_key().secret) == 32


def test_req_320b_keys_come_from_the_csprng(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def choice(seq: str) -> str:
        calls.append(seq)
        return seq[0]

    monkeypatch.setattr(std_secrets, "choice", choice)  # the module security draws from
    key = security.generate_api_key()
    assert len(calls) == 8 + 32
    assert key.plaintext == "al_00000000_" + "0" * 32


def test_req_320b_keys_are_unique() -> None:
    assert len({security.generate_api_key().plaintext for _ in range(500)}) == 500


def test_req_320a_hash_never_equals_or_contains_plaintext() -> None:
    key = security.generate_api_key()
    stored = security.hash_secret(key.secret)
    assert len(stored.salt) == 16
    for blob in (
        stored.digest,
        stored.salt,
        stored.digest.hex().encode(),
        stored.salt.hex().encode(),
    ):
        assert key.secret.encode() not in blob
        assert key.plaintext.encode() not in blob
    assert key.secret not in repr(stored)
    assert key.secret not in repr(key)


def test_req_320a_verification_accepts_the_key_and_rejects_others() -> None:
    key = security.generate_api_key()
    stored = security.hash_secret(key.secret)
    assert security.verify_secret(key.secret, stored)
    assert not security.verify_secret(security.generate_api_key().secret, stored)
    last = "y" if key.secret.endswith("x") else "x"  # always a different final character
    assert not security.verify_secret(key.secret[:-1] + last, stored)


def test_req_320a_same_secret_different_salt_different_hash() -> None:
    a, b = security.hash_secret("s" * 32), security.hash_secret("s" * 32)
    assert a.salt != b.salt
    assert a.digest != b.digest


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "al_",
        "al_abcdefgh",
        "xx_abcdefgh_" + "A" * 32,
        "al_ABCDEFGH_" + "A" * 32,
        "al_abcdefgh_" + "A" * 31,
    ],
)
def test_malformed_keys_are_rejected_without_echoing_them(bad: str) -> None:
    with pytest.raises(ValidationError) as info:
        security.parse_api_key(bad)
    assert bad == "" or bad not in str(info.value)


def test_req_320a_signed_tokens_round_trip_and_detect_tampering() -> None:
    token = security.sign(KEY, "cursor", b'{"after": 42}')
    assert security.unsign(KEY, "cursor", token) == b'{"after": 42}'
    body, mac = token.split(".")
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + mac
    with pytest.raises(ValidationError):
        security.unsign(KEY, "cursor", tampered)
    with pytest.raises(ValidationError):
        security.unsign(KEY, "session", token)  # purpose separation
    with pytest.raises(ValidationError):
        security.unsign(std_secrets.token_bytes(32), "cursor", token)  # wrong key
    with pytest.raises(ValidationError):
        security.unsign(KEY, "cursor", "not-a-token")


def test_short_signing_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        security.sign(b"short", "cursor", b"x")
