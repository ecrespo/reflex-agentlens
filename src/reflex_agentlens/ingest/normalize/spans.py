"""OTLP span → draft, Source, truncation and size (plan task P3-02.2).

REQ-030.a/b (raw attributes kept, typed, after redaction), REQ-031.a/b (Source), REQ-041 (value
size limit), Data Model §2.10 (column shapes). Pure functions: no I/O.

A :class:`SpanDraft` is the span between decoding and the final ``NormalizedSpan``: redaction
(``ingest.redaction``) rewrites its string leaves, then :func:`truncate` caps them (Tech Design
v1.4 Flow 1: redact **before** truncating, Analyze A-07), then content drop and derivation run.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.store.attrs import (
    JsonValue,
    attributes_to_json,
    attrs_hash,
    canonical_json,
)
from reflex_agentlens.store.records import ResourceIn, SourceKey

__all__ = [
    "FLAG_CONTENT_DROPPED",
    "FLAG_REDACTED",
    "FLAG_REDACTION_TIMEOUT",
    "FLAG_TRUNCATED",
    "UNKNOWN_SERVICE",
    "SpanDraft",
    "draft",
    "map_strings",
    "resource_in",
    "size_bytes",
    "source_of",
    "truncate",
]

# spans.flags bitmask (Data Model §2.10)
FLAG_TRUNCATED = 1
FLAG_REDACTED = 2
FLAG_CONTENT_DROPPED = 4
FLAG_REDACTION_TIMEOUT = 8
UNKNOWN_SERVICE = "unknown_service"


@dataclass(frozen=True, slots=True)
class SpanDraft:
    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes | None
    name: str
    otel_kind: int
    start_ns: int
    end_ns: int
    status_code: int
    status_message: str | None
    attributes: dict[str, JsonValue]
    events: list[dict[str, JsonValue]]
    links: list[dict[str, JsonValue]]
    resource: dict[str, JsonValue]
    resource_schema_url: str | None
    scope_name: str | None
    scope_version: str | None
    received_at_ns: int
    dropped_counts: dict[str, JsonValue] = field(default_factory=dict)
    dropped_keys: list[str] = field(default_factory=list)
    flags: int = 0


def source_of(resource: dict[str, JsonValue]) -> SourceKey:
    """REQ-031: ``service.name`` (+ namespace); ``unknown_service`` when absent or empty."""
    name = resource.get(k.SERVICE_NAME)
    namespace = resource.get(k.SERVICE_NAMESPACE)
    service = name if isinstance(name, str) and name else UNKNOWN_SERVICE
    return SourceKey(service, namespace if isinstance(namespace, str) else "")


def resource_in(draft_: SpanDraft) -> ResourceIn:
    return ResourceIn(attrs_hash(draft_.resource), draft_.resource, draft_.resource_schema_url)


def _dropped(**counts: int) -> dict[str, JsonValue]:
    return {name: n for name, n in counts.items() if n}


def draft(
    span: Span,
    resource: Resource,
    scope: InstrumentationScope,
    received_at_ns: int,
    resource_schema_url: str = "",
) -> SpanDraft:
    """REQ-030.a: every OTLP field the store keeps, attributes as typed JSON."""
    events: list[dict[str, JsonValue]] = [
        {
            "name": ev.name,
            "time_ns": ev.time_unix_nano,
            "attributes": attributes_to_json(ev.attributes),
        }
        for ev in span.events
    ]
    links: list[dict[str, JsonValue]] = [
        {
            "trace_id_hex": link.trace_id.hex(),
            "span_id_hex": link.span_id.hex(),
            "attributes": attributes_to_json(link.attributes),
        }
        for link in span.links
    ]
    return SpanDraft(
        trace_id=bytes(span.trace_id),
        span_id=bytes(span.span_id),
        parent_span_id=bytes(span.parent_span_id) or None,
        name=span.name,
        otel_kind=int(span.kind),
        start_ns=span.start_time_unix_nano,
        end_ns=span.end_time_unix_nano,
        status_code=int(span.status.code),
        status_message=span.status.message or None,
        attributes=attributes_to_json(span.attributes),
        events=events,
        links=links,
        resource=attributes_to_json(resource.attributes),
        resource_schema_url=resource_schema_url or None,
        scope_name=scope.name or None,
        scope_version=scope.version or None,
        received_at_ns=received_at_ns,
        dropped_counts=_dropped(
            attributes=span.dropped_attributes_count,
            events=span.dropped_events_count,
            links=span.dropped_links_count,
        ),
    )


# ── string leaves (shared by redaction and truncation) ───────────────────────────────────────


def _map_value(value: JsonValue, fn: Callable[[str], str]) -> JsonValue:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_map_value(v, fn) for v in value]
    if isinstance(value, dict):
        if set(value) == {"$b64"}:  # bytes value (store.attrs codec): not text
            return value
        return {key: _map_value(v, fn) for key, v in value.items()}
    return value


def _map_attrs(attrs: dict[str, JsonValue], fn: Callable[[str], str]) -> dict[str, JsonValue]:
    return {key: _map_value(v, fn) for key, v in attrs.items()}


def map_strings(d: SpanDraft, fn: Callable[[str], str], *, resource: bool = True) -> SpanDraft:
    """Apply ``fn`` to every string leaf: span, event, link and resource attributes, status."""
    events = [{**ev, "attributes": _map_attrs(_obj(ev.get("attributes")), fn)} for ev in d.events]
    links = [{**ln, "attributes": _map_attrs(_obj(ln.get("attributes")), fn)} for ln in d.links]
    return dataclasses.replace(
        d,
        attributes=_map_attrs(d.attributes, fn),
        events=events,
        links=links,
        resource=_map_attrs(d.resource, fn) if resource else d.resource,
        status_message=None if d.status_message is None else fn(d.status_message),
    )


def _obj(value: Any) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


# ── truncation (REQ-041) ─────────────────────────────────────────────────────────────────────


def _cut(value: str, limit: int) -> str | None:
    data = value.encode()
    if len(data) <= limit:
        return None
    kept = data[:limit].decode("utf-8", errors="ignore")  # never split a character
    return f"{kept}…[truncated {len(data) - len(kept.encode())} bytes]"


def truncate(d: SpanDraft, limit_bytes: int) -> SpanDraft:
    """REQ-041.a: values over the limit are cut, marked, and the span flagged ``truncated``."""
    hit = False

    def cap(value: str) -> str:
        nonlocal hit
        cut = _cut(value, limit_bytes)
        if cut is None:
            return value
        hit = True
        return cut

    out = map_strings(d, cap)
    return dataclasses.replace(out, flags=out.flags | FLAG_TRUNCATED) if hit else d


def size_bytes(d: SpanDraft) -> int:
    """Serialized size of what the span row stores (retention estimate, Data Model §2.10)."""
    body = canonical_json([d.attributes, d.events, d.links, d.dropped_counts, d.dropped_keys])
    text = len(d.name.encode()) + len((d.status_message or "").encode())
    return len(body.encode()) + text + 64  # + fixed-width columns
