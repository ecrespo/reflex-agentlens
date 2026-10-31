"""UTC and Unix-nanosecond helpers (Constitution Art. 5, API §2).

Storage and DTO timestamps are ``int`` Unix nanoseconds, UTC. Config durations are
``"500ms" | "30s" | "5m" | "1h" | "30d"`` or integer seconds.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

from reflex_agentlens.core.errors import ValidationError

__all__ = [
    "NS_PER_MS",
    "NS_PER_S",
    "NS_PER_US",
    "datetime_to_ns",
    "format_duration",
    "now_ns",
    "ns_to_datetime",
    "ns_to_rfc3339",
    "parse_duration",
    "parse_rfc3339",
]

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DURATION_RE = re.compile(r"(\d+)(ms|s|m|h|d)?")
_UNITS = {
    "ms": timedelta(milliseconds=1),
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def now_ns() -> int:
    return time.time_ns()


def datetime_to_ns(dt: datetime) -> int:
    """Exact conversion of an aware datetime; naive datetimes are rejected."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationError("timestamp", "naive datetime (no UTC offset)")
    delta = dt - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * NS_PER_S + delta.microseconds * NS_PER_US


def ns_to_datetime(ns: int) -> datetime:
    """UTC datetime; sub-microsecond precision is truncated (datetime has µs resolution)."""
    return _EPOCH + timedelta(microseconds=ns // NS_PER_US)


def ns_to_rfc3339(ns: int) -> str:
    """``2026-10-30T12:00:00.123Z`` (millisecond precision, as in logs and URLs)."""
    dt = ns_to_datetime(ns)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_rfc3339(text: str) -> int:
    """RFC 3339 timestamp with an explicit offset (``Z`` or ``±hh:mm``) → Unix ns."""
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("timestamp", "not RFC 3339") from None
    if dt.tzinfo is None:
        raise ValidationError("timestamp", "RFC 3339 timestamp needs an offset")
    return datetime_to_ns(dt)


def parse_duration(value: object) -> timedelta:
    """Parse a config duration. Integers (and integer strings) are seconds."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValidationError("duration", "expected a duration string or integer seconds")
    if isinstance(value, int):
        if value < 0:
            raise ValidationError("duration", "negative")
        return timedelta(seconds=value)
    m = _DURATION_RE.fullmatch(value.strip())
    if not m:
        raise ValidationError("duration", "expected e.g. 500ms, 30s, 5m, 1h, 30d")
    return int(m.group(1)) * _UNITS[m.group(2) or "s"]


def format_duration(td: timedelta) -> str:
    """Shortest exact representation in the config syntax."""
    ms = td // timedelta(milliseconds=1)
    for unit, size in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)):
        if ms % size == 0 and ms:
            return f"{ms // size}{unit}"
    return f"{ms}ms" if ms else "0s"
