"""Small coercions shared by the per-span derivations (plan tasks P3-03.1, P3-03.2).

Attribute values come from arbitrary instrumentations, so every derived column is coerced
defensively: counts must be non-negative integers, text is cut to its column width
(Data Model §2.11-2.13), and anything else becomes ``None`` rather than an error.
"""

from __future__ import annotations

import math
from typing import Any

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.ingest.normalize.spans import FLAG_CONTENT_DROPPED, SpanDraft
from reflex_agentlens.store.records import OkError

__all__ = ["STATUS_ERROR", "content_dropped", "count", "outcome", "text"]

STATUS_ERROR = 2  # OTLP Status.STATUS_CODE_ERROR
_MAX_COUNT = 2**63 - 1


def count(value: Any) -> int | None:
    """A token count: a non-negative int (``"12"`` and ``12.0`` accepted), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isascii() or not value.isdigit() or len(value) > 19:
            return None
        value = int(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int) and 0 <= value <= _MAX_COUNT:
        return value
    return None


def text(value: Any, width: int) -> str | None:
    """A non-empty string cut to ``width`` characters, else ``None``."""
    if isinstance(value, str) and value:
        return value[:width]
    return None


def _exception_type(d: SpanDraft) -> str | None:
    for ev in d.events:
        attrs = ev.get("attributes")
        if ev.get("name") == k.EVENT_EXCEPTION and isinstance(attrs, dict):
            found = text(attrs.get(k.EXCEPTION_TYPE), 128)
            if found is not None:
                return found
    return None


def outcome(d: SpanDraft) -> tuple[OkError, str | None]:
    """Status and error type: ``error.type``, else the exception event of an ERROR span."""
    error_type = text(d.attributes.get(k.ERROR_TYPE), 128)
    if d.status_code == STATUS_ERROR:
        return "error", error_type or _exception_type(d)
    return ("error", error_type) if error_type is not None else ("ok", None)


def content_dropped(d: SpanDraft) -> bool:
    return bool(d.flags & FLAG_CONTENT_DROPPED)
