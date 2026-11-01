"""Signed keyset cursors (plan task P4-01.3; API §6.2, REQ-100.a, REQ-105.a).

A cursor is ``core.security.sign`` (HMAC-SHA256, purpose ``cursor``) over a compact JSON payload
that carries its kind and sort, so a forged, truncated or foreign cursor, or one used with another
sort, raises ``ValidationError("cursor")`` instead of returning an arbitrary page.
"""

from __future__ import annotations

import json
from typing import Any

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.ids import trace_id_from_hex, trace_id_to_hex
from reflex_agentlens.core.security import sign, unsign
from reflex_agentlens.query.dto import RunSort, SessionSort
from reflex_agentlens.store.records import RunCursor, SessionCursor

__all__ = ["CursorCodec"]

_PURPOSE = "cursor"
_MAX_CURSOR = 1024


def _bad() -> ValidationError:
    return ValidationError("cursor", "invalid or expired cursor")


class CursorCodec:
    """Encodes and verifies Run and Session cursors under one signing key (≥ 32 bytes)."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("cursor key must be at least 32 bytes")
        self._key = key

    def _encode(self, payload: list[Any]) -> str:
        data = json.dumps(payload, separators=(",", ":")).encode()
        return sign(self._key, _PURPOSE, data)

    def _decode(self, token: str, kind: str, sort: str) -> list[Any]:
        if not token or len(token) > _MAX_CURSOR:
            raise _bad()
        try:
            payload = json.loads(unsign(self._key, _PURPOSE, token))
        except (ValidationError, ValueError, UnicodeDecodeError):
            raise _bad() from None
        if not isinstance(payload, list) or len(payload) < 3 or payload[:2] != [kind, sort]:
            raise _bad()
        return payload

    def encode_run(self, sort: RunSort, cursor: RunCursor) -> str:
        return self._encode(
            ["run", sort, cursor.sort_value, trace_id_to_hex(cursor.trace_id), cursor.project_id]
        )

    def decode_run(self, sort: RunSort, token: str) -> RunCursor:
        p = self._decode(token, "run", sort)
        if len(p) != 5 or type(p[2]) is not int or not all(isinstance(x, str) for x in p[3:]):
            raise _bad()
        try:
            trace_id = trace_id_from_hex(p[3])
        except ValidationError:
            raise _bad() from None
        return RunCursor(sort_value=p[2], trace_id=trace_id, project_id=p[4])

    def encode_session(self, sort: SessionSort, cursor: SessionCursor) -> str:
        return self._encode(["session", sort, cursor.sort_value, cursor.session_id])

    def decode_session(self, sort: SessionSort, token: str) -> SessionCursor:
        p = self._decode(token, "session", sort)
        if len(p) != 4 or type(p[2]) is not int or not isinstance(p[3], str):
            raise _bad()
        return SessionCursor(sort_value=p[2], session_id=p[3])
