"""Self-telemetry recording points (plan task P5-07.1; REQ-342, 323.a, Tech Design §7.2).

The layers record through :func:`recorder`, which is a no-op until the app installs an exporter
(``app.selftelemetry``, extra ``[selftelemetry]``). OpenTelemetry stays out of the core layers.

Content-free by construction: every metric, span and label key is declared below, and every
label value comes from a closed set (:data:`LABEL_VALUES`). An undeclared name or value is a bug
in the caller; the exporter drops the name and maps an unknown value to ``other``, so no
ingested value (span names, attributes, project names) can ever leave the process this way.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Literal, ParamSpec, TypeVar

__all__ = [
    "LABEL_VALUES",
    "METRICS",
    "SPANS",
    "Recorder",
    "install",
    "outcome",
    "recorder",
    "timed",
]

Kind = Literal["counter", "histogram", "gauge"]

# name → (kind, unit, description, label keys)
METRICS: dict[str, tuple[Kind, str, str, frozenset[str]]] = {
    "agentlens.ingest.requests": (
        "counter", "{request}", "OTLP export requests", frozenset({"protocol", "outcome"})
    ),
    "agentlens.ingest.spans": ("counter", "{span}", "Spans by outcome", frozenset({"outcome"})),
    "agentlens.ingest.metrics.discarded": (
        "counter", "{point}", "Non-GenAI metric points discarded", frozenset()
    ),
    "agentlens.ingest.queue.bytes": ("gauge", "By", "WriteQueue occupancy", frozenset()),
    "agentlens.write.commit.duration": ("histogram", "s", "Group commit latency", frozenset()),
    "agentlens.write.group.spans": ("histogram", "{span}", "Spans per group", frozenset()),
    "agentlens.query.duration": ("histogram", "s", "Query-layer latency", frozenset({"query"})),
    "agentlens.redaction.timeouts": (
        "counter", "{span}", "Spans with a fail-closed redaction replacement", frozenset()
    ),
    "agentlens.changefeed.lag": ("histogram", "s", "Commit to UI notice", frozenset()),
}  # fmt: skip

# span name → attribute keys
SPANS: dict[str, frozenset[str]] = {
    "agentlens.ingest.export": frozenset({"protocol", "outcome"}),
    "agentlens.write.commit": frozenset(),
    "agentlens.query": frozenset({"query"}),
}

QUERIES = frozenset({
    "list_runs", "resolve_trace", "get_trace", "get_graph", "list_llm_calls", "get_span_raw",
    "get_span_detail", "list_sessions", "get_session", "facets", "list_projects", "list_sources",
    "get_project", "dashboard", "storage_status",
})  # fmt: skip

OUTCOMES = frozenset({
    "accepted", "rejected_auth", "rate_limited", "too_large", "overloaded", "invalid",
    "rejected", "duplicate", "truncated", "error",
})  # fmt: skip

LABEL_VALUES: dict[str, frozenset[str]] = {
    "protocol": frozenset({"http/protobuf", "http/json", "grpc"}),
    "outcome": OUTCOMES,
    "query": QUERIES,
}

_OUTCOMES = {401: "rejected_auth", 429: "rate_limited", 413: "too_large", 503: "overloaded"}


def outcome(http_status: int) -> str:
    """The ``agentlens.ingest.requests`` outcome of an HTTP status (API §3.3)."""
    if http_status == 200:
        return "accepted"
    if http_status >= 500 and http_status != 503:
        return "error"
    return _OUTCOMES.get(http_status, "invalid")


class Recorder:
    """The no-op recorder; the exporter in ``app.selftelemetry`` overrides every method.

    Implementations must be thread-safe: the ingest engine records from its own thread.
    """

    def add(self, name: str, value: int = 1, labels: Mapping[str, str] | None = None) -> None:
        """Add to a counter."""

    def record(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Record a histogram sample."""

    def set(self, name: str, value: float) -> None:
        """Set a gauge."""

    @contextmanager
    def span(self, name: str, attributes: dict[str, str] | None = None) -> Iterator[None]:
        """A span around the block; ``attributes`` may be filled in before the block ends."""
        yield


_NOOP = Recorder()
_current: Recorder = _NOOP


def recorder() -> Recorder:
    return _current


def install(new: Recorder | None) -> None:
    """Install the process recorder; ``None`` restores the no-op one."""
    global _current
    _current = new or _NOOP


P = ParamSpec("P")
T = TypeVar("T")


def timed(query: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Time an async query method into ``agentlens.query.duration{query}`` and a span."""
    if query not in QUERIES:
        raise ValueError(f"undeclared query label {query!r}")
    labels = {"query": query}

    def wrap(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T:
            rec = _current
            if rec is _NOOP:
                return await fn(*args, **kwargs)
            started = time.perf_counter()
            try:
                with rec.span("agentlens.query", dict(labels)):
                    return await fn(*args, **kwargs)
            finally:
                rec.record("agentlens.query.duration", time.perf_counter() - started, labels)

        return inner

    return wrap
