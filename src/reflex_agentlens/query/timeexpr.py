"""Time expressions of URLs and filters (plan task P4-01.2; API §2, §6.2, REQ-101.b).

``now``, ``now-<n><unit>`` (units ``s``, ``m``, ``h``, ``d``, ``w``) or an RFC 3339 timestamp with
an explicit offset. Relative expressions resolve against an injected ``now_ns``.
"""

from __future__ import annotations

import re

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.timeutil import NS_PER_S, parse_rfc3339

__all__ = ["MAX_TIME_EXPR", "check_time_expr", "resolve_time"]

MAX_TIME_EXPR = 64
_RELATIVE = re.compile(r"now(?:-(\d{1,6})([smhdw]))?")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}


def resolve_time(expr: str, now_ns: int, field: str) -> int:
    """Unix ns of ``expr``; a malformed value raises ``ValidationError(field)`` (REQ-101.i)."""
    text = expr.strip()
    if not text or len(text) > MAX_TIME_EXPR:
        raise ValidationError(field, "expected RFC 3339 or now-<n><s|m|h|d|w>")
    m = _RELATIVE.fullmatch(text)
    if m:
        if m.group(1) is None:
            return now_ns
        return now_ns - int(m.group(1)) * _UNIT_S[m.group(2)] * NS_PER_S
    try:
        ns = parse_rfc3339(text)
    except ValidationError:
        raise ValidationError(field, "expected RFC 3339 or now-<n><s|m|h|d|w>") from None
    if ns < 0:
        raise ValidationError(field, "before 1970")
    return ns


def check_time_expr(expr: str, field: str) -> str:
    """Validate the syntax only (URL parsing); returns the stripped expression."""
    resolve_time(expr, 0, field)
    return expr.strip()
