"""Session external ID (plan task P3-03.3; REQ-036.a/c, Data Model §2.8, Analyze A-08).

The ID is read from the mapping **after** redaction (rules and hook). If redaction changed it,
the stored value is ``<redacted>~hex(HMAC-SHA256(session_key, raw))[:16]``: two conversations
whose IDs redact to the same text (two emails) stay two Sessions, and the raw ID is never
stored. Values longer than 512 characters become ``trunc(480) + '…' + hex(blake2b-64)``.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from reflex_agentlens.core.semconv.mapping import resolve
from reflex_agentlens.store.records import SessionKey

__all__ = ["MAX_EXTERNAL_ID", "external_id", "session_key"]

MAX_EXTERNAL_ID = 512
_KEEP = 480


def external_id(value: Any) -> str | None:
    """A conversation ID as text: non-empty strings and integers only."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _cap(value: str) -> str:
    if len(value) <= MAX_EXTERNAL_ID:
        return value
    digest = hashlib.blake2b(value.encode(), digest_size=8).hexdigest()
    return f"{value[:_KEEP]}…{digest}"


def session_key(
    raw_attrs: dict[str, Any], redacted_attrs: dict[str, Any], secret: bytes
) -> SessionKey | None:
    """``raw_attrs`` before redaction, ``redacted_attrs`` after; ``secret`` from ``app_meta``."""
    redacted = external_id(resolve(redacted_attrs, "session_id"))
    if redacted is None:
        return None
    raw = external_id(resolve(raw_attrs, "session_id"))
    if raw is not None and raw != redacted:
        tag = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:16]
        redacted = f"{redacted}~{tag}"
    return SessionKey(_cap(redacted))
