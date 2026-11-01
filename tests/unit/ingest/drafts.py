"""A ``SpanDraft`` builder for derivation tests (attributes as the decoded JSON form)."""

from __future__ import annotations

import dataclasses
from typing import Any

from reflex_agentlens.ingest.normalize.spans import SpanDraft
from tests.unit.ingest.otlp import NOW_NS, S

BASE = SpanDraft(
    trace_id=b"\x01" * 16, span_id=b"\x02" * 8, parent_span_id=None, name="op", otel_kind=0,
    start_ns=NOW_NS - 10 * S, end_ns=NOW_NS - 7 * S, status_code=0, status_message=None,
    attributes={}, events=[], links=[], resource={"service.name": "svc"},
    resource_schema_url=None, scope_name="t", scope_version=None, received_at_ns=NOW_NS,
)  # fmt: skip


def mk(attrs: dict[str, Any] | None = None, **fields: Any) -> SpanDraft:
    return dataclasses.replace(BASE, attributes=dict(attrs or {}), **fields)


def event(name: str, **attrs: Any) -> dict[str, Any]:
    return {"name": name, "time_ns": NOW_NS - 8 * S, "attributes": attrs}
