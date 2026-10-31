"""OTel trace/span ID conversion and validation, and ULIDs (API §2, Data Model §1.3).

OTel IDs are stored as bytes (trace 16, span 8) and shown as lowercase hex. An all-zero or
wrong-length ID is invalid [REQ-010.a]. Public entity IDs are ULIDs: 48-bit millisecond timestamp
plus 80 random bits, 26 chars of Crockford base32 (uppercase), lexicographically time-ordered.
"""

from __future__ import annotations

import re
import secrets
import time

from reflex_agentlens.core.errors import ValidationError

__all__ = [
    "SPAN_ID_BYTES",
    "TRACE_ID_BYTES",
    "is_ulid",
    "is_valid_span_id",
    "is_valid_trace_id",
    "new_ulid",
    "span_id_from_hex",
    "span_id_to_hex",
    "trace_id_from_hex",
    "trace_id_to_hex",
    "ulid_timestamp_ms",
]

TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_HEX_RE = re.compile(r"[0-9a-fA-F]*")


def _valid(raw: bytes, size: int) -> bool:
    return len(raw) == size and any(raw)


def is_valid_trace_id(raw: bytes) -> bool:
    """16 bytes and not all zero [REQ-010.a]."""
    return _valid(raw, TRACE_ID_BYTES)


def is_valid_span_id(raw: bytes) -> bool:
    """8 bytes and not all zero [REQ-010.a]."""
    return _valid(raw, SPAN_ID_BYTES)


def _from_hex(text: str, size: int, field: str) -> bytes:
    if len(text) != size * 2:
        raise ValidationError(field, f"expected {size * 2} hex chars, got {len(text)}")
    if not _HEX_RE.fullmatch(text):
        raise ValidationError(field, "not hexadecimal")
    raw = bytes.fromhex(text)
    if not any(raw):
        raise ValidationError(field, "all-zero")
    return raw


def trace_id_from_hex(text: str) -> bytes:
    return _from_hex(text, TRACE_ID_BYTES, "trace_id")


def span_id_from_hex(text: str) -> bytes:
    return _from_hex(text, SPAN_ID_BYTES, "span_id")


def trace_id_to_hex(raw: bytes) -> str:
    return raw.hex()


def span_id_to_hex(raw: bytes) -> str:
    return raw.hex()


def new_ulid(now_ms: int | None = None) -> str:
    """A new ULID. ``now_ms`` (Unix milliseconds) is injectable for tests."""
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if not 0 <= ms < 1 << 48:
        raise ValidationError("ulid", "timestamp out of range")
    value = (ms << 80) | secrets.randbits(80)
    return "".join(CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def is_ulid(text: str) -> bool:
    return bool(_ULID_RE.fullmatch(text))


def ulid_timestamp_ms(text: str) -> int:
    if not is_ulid(text):
        raise ValidationError("ulid", "not a ULID")
    value = 0
    for ch in text:
        value = (value << 5) | CROCKFORD.index(ch)
    return value >> 80
