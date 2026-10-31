"""Span validation and ``partialSuccess`` (plan task P3-02.1; REQ-010.a/b, API §3.3, §3.6).

Rules in API §3.6 order; the first failing rule is the span's rejection reason. Clock skew rules
``future_timestamp`` and ``older_than_retention`` come from Analyze A-17 (API v1.3). Reasons are
fixed keys; the ``partialSuccess`` message lists counts per reason and never span content.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "RejectReason",
    "Rejections",
    "SpanTimes",
    "ValidationLimits",
    "validate_span",
]

RejectReason = Literal[
    "invalid_trace_id",
    "invalid_span_id",
    "invalid_parent_span_id",
    "missing_start_time",
    "end_before_start",
    "future_timestamp",
    "older_than_retention",
    "redaction_hook_error",
]
NS_PER_S = 1_000_000_000
_ZERO16, _ZERO8 = bytes(16), bytes(8)


class SpanTimes(Protocol):
    """The fields validation reads (an OTLP ``Span`` message satisfies it)."""

    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes
    start_time_unix_nano: int
    end_time_unix_nano: int


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    received_at_ns: int
    max_clock_skew_ns: int = 3_600 * NS_PER_S  # ingest.max_clock_skew, default 1 h
    retention_max_age_s: int = 0  # the Project's; 0 = keep forever


def validate_span(span: SpanTimes, limits: ValidationLimits) -> RejectReason | None:
    if len(span.trace_id) != 16 or span.trace_id == _ZERO16:
        return "invalid_trace_id"
    if len(span.span_id) != 8 or span.span_id == _ZERO8:
        return "invalid_span_id"
    if span.parent_span_id and len(span.parent_span_id) != 8:
        return "invalid_parent_span_id"
    start, end = span.start_time_unix_nano, span.end_time_unix_nano
    if start == 0:
        return "missing_start_time"
    if end < start:  # an unended span (end == 0) is invalid in OTLP export semantics
        return "end_before_start"
    if start > limits.received_at_ns + limits.max_clock_skew_ns:
        return "future_timestamp"
    if limits.retention_max_age_s > 0:
        # clamp: ages beyond "now" put the cutoff before the epoch (no overflow, no rejection)
        age_s = min(limits.retention_max_age_s, limits.received_at_ns // NS_PER_S)
        if end < limits.received_at_ns - age_s * NS_PER_S:
            return "older_than_retention"
    return None


class Rejections:
    """Rejected items per reason, rendered as the ``partialSuccess`` message."""

    def __init__(self, unit: Literal["spans", "data points"] = "spans") -> None:
        self.unit = unit
        self._counts: Counter[str] = Counter()

    def add(self, reason: str, n: int = 1) -> None:
        self._counts[reason] += n

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def message(self) -> str:
        """``"3 spans rejected: invalid_span_id=2, end_before_start=1"`` (count desc, then key)."""
        if not self._counts:
            return ""
        ordered = sorted(self._counts.items(), key=lambda item: (-item[1], item[0]))
        detail = ", ".join(f"{reason}={n}" for reason, n in ordered)
        return f"{self.total} {self.unit} rejected: {detail}"
