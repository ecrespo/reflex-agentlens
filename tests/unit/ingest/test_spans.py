"""Span drafts: Source, raw attributes, truncation, classification (plan task P3-02.2).

REQ-030.a/b, REQ-031.a/b, REQ-041.a.
"""

from __future__ import annotations

import dataclasses

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, InstrumentationScope
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from reflex_agentlens.ingest.normalize.classify import classify_draft
from reflex_agentlens.ingest.normalize.spans import (
    FLAG_TRUNCATED,
    UNKNOWN_SERVICE,
    SpanDraft,
    draft,
    map_strings,
    resource_in,
    size_bytes,
    source_of,
    truncate,
)
from reflex_agentlens.store.attrs import attributes_from_json
from reflex_agentlens.store.records import SourceKey
from tests.unit.ingest.otlp import NOW_NS, kv, span


def make(sp: Span | None = None, **resource: object) -> SpanDraft:
    res = Resource(attributes=[kv(key.replace("__", "."), v) for key, v in resource.items()])
    scope = InstrumentationScope(name="pydantic-ai", version="1.2")
    return draft(sp or span(), res, scope, NOW_NS, "https://opentelemetry.io/schemas/1.44.0")


def test_draft_keeps_every_field() -> None:
    """REQ-030.a/b: identity, timing, status, typed attributes, events, links, scope."""
    sp = span(3, 4, 2, gen_ai__operation__name="chat", n=7, ratio=0.5, ok=True)
    sp.kind = Span.SPAN_KIND_CLIENT
    sp.status.CopyFrom(Status(code=Status.STATUS_CODE_ERROR, message="boom"))
    ev = sp.events.add(name="gen_ai.choice", time_unix_nano=NOW_NS)
    ev.attributes.append(kv("index", 0))
    link = sp.links.add(trace_id=(9).to_bytes(16, "big"), span_id=(8).to_bytes(8, "big"))
    link.attributes.append(kv("why", "retry"))
    arr = sp.attributes.add(key="list")
    arr.value.CopyFrom(AnyValue(array_value=ArrayValue(values=[AnyValue(string_value="a")])))
    sp.dropped_attributes_count = 2
    d = make(sp, service__name="agent")
    assert d.trace_id == (3).to_bytes(16, "big")
    assert d.parent_span_id == (2).to_bytes(8, "big")
    assert (d.otel_kind, d.status_code, d.status_message) == (3, 2, "boom")
    assert d.attributes == {
        "gen_ai.operation.name": "chat", "n": 7, "ratio": 0.5, "ok": True, "list": ["a"],
    }  # fmt: skip
    assert d.events == [{"name": "gen_ai.choice", "time_ns": NOW_NS,
                         "attributes": {"index": 0}}]  # fmt: skip
    assert d.links == [{"trace_id_hex": "00" * 15 + "09", "span_id_hex": "00" * 7 + "08",
                        "attributes": {"why": "retry"}}]  # fmt: skip
    assert (d.scope_name, d.scope_version) == ("pydantic-ai", "1.2")
    assert d.dropped_counts == {"attributes": 2}
    # raw attributes round-trip back to the OTLP input (REQ-030.b)
    assert attributes_from_json(d.attributes) == list(sp.attributes)
    res = resource_in(d)
    assert res.attributes == {"service.name": "agent"}
    assert res.schema_url == "https://opentelemetry.io/schemas/1.44.0"


def test_root_span_has_no_parent_and_empty_status() -> None:
    d = make(span(1, 1, None))
    assert d.parent_span_id is None
    assert d.status_message is None
    assert d.dropped_counts == {}


def test_source_identification() -> None:
    """REQ-031.a/b."""
    assert source_of({"service.name": "a", "service.namespace": "shop"}) == SourceKey("a", "shop")
    assert source_of({"service.name": "a"}) == SourceKey("a")
    assert source_of({}) == SourceKey(UNKNOWN_SERVICE)
    assert source_of({"service.name": ""}) == SourceKey(UNKNOWN_SERVICE)
    assert source_of({"service.name": 5}) == SourceKey(UNKNOWN_SERVICE)


def test_truncation_marks_and_flags() -> None:
    """REQ-041.a: cut at the limit, marker with the dropped byte count, span flagged."""
    sp = span(big="x" * 300, small="ok", nested="é" * 200)
    sp.status.message = "y" * 300
    ev = sp.events.add(name="e")
    ev.attributes.append(kv("payload", "z" * 300))
    d = truncate(make(sp, service__name="s", huge="w" * 300), 100)
    assert d.flags & FLAG_TRUNCATED
    assert d.attributes["big"] == "x" * 100 + "…[truncated 200 bytes]"
    assert d.attributes["small"] == "ok"
    nested = d.attributes["nested"]
    assert isinstance(nested, str)
    assert nested.startswith("é" * 50)  # 100 bytes = 50 two-byte characters, none split
    assert nested.endswith("…[truncated 300 bytes]")
    assert d.status_message == "y" * 100 + "…[truncated 200 bytes]"
    assert d.events[0]["attributes"] == {"payload": "z" * 100 + "…[truncated 200 bytes]"}
    assert d.resource["huge"] == "w" * 100 + "…[truncated 200 bytes]"


def test_no_truncation_returns_the_same_draft() -> None:
    d = make(span(a="short"))
    assert truncate(d, 100) is d


def test_bytes_values_are_not_text() -> None:
    d = dataclasses.replace(make(span()), attributes={"b": {"$b64": "AAAA"}, "s": "t"})
    assert map_strings(d, str.upper).attributes == {"b": {"$b64": "AAAA"}, "s": "T"}


def test_size_and_classification() -> None:
    small = make(span(a="x"))
    large = make(span(a="x" * 1000))
    assert size_bytes(large) - size_bytes(small) == 999
    assert classify_draft(make(span(gen_ai__operation__name="chat"))).category == "llm"
    assert classify_draft(make(span(http__method="GET"))).category == "generic"
