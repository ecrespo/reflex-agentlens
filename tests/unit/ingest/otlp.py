"""Builders for OTLP test requests (protobuf messages and their OTLP/JSON form)."""

from __future__ import annotations

import base64
import json
from typing import Any

from google.protobuf import json_format  # type: ignore[import-untyped]
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span

NOW_NS = 1_793_000_000_000_000_000
S = 1_000_000_000


def kv(key: str, value: Any) -> KeyValue:
    if isinstance(value, bool):
        return KeyValue(key=key, value=AnyValue(bool_value=value))
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    if isinstance(value, float):
        return KeyValue(key=key, value=AnyValue(double_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


def span(
    trace: int = 1,
    sid: int = 1,
    parent: int | None = None,
    start_ago_s: int = 10,
    end_ago_s: int = 5,
    name: str = "op",
    **attrs: Any,
) -> Span:
    return Span(
        trace_id=trace.to_bytes(16, "big"),
        span_id=sid.to_bytes(8, "big"),
        parent_span_id=b"" if parent is None else parent.to_bytes(8, "big"),
        name=name,
        start_time_unix_nano=NOW_NS - start_ago_s * S,
        end_time_unix_nano=NOW_NS - end_ago_s * S,
        attributes=[kv(k.replace("__", "."), v) for k, v in attrs.items()],
    )


def request(*spans: Span, service: str = "svc", **resource: Any) -> ExportTraceServiceRequest:
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(kv("service.name", service))
    for key, value in resource.items():
        rs.resource.attributes.append(kv(key.replace("__", "."), value))
    ss = rs.scope_spans.add()
    ss.scope.name = "test.scope"
    ss.spans.extend(spans)
    return req


def to_otlp_json(req: ExportTraceServiceRequest) -> bytes:
    """OTLP/JSON: protobuf JSON mapping with hex trace and span IDs."""
    doc = json_format.MessageToDict(req)
    for rs in doc.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                for name in ("traceId", "spanId", "parentSpanId"):
                    if name in sp:
                        sp[name] = base64.b64decode(sp[name]).hex()
    return json.dumps(doc).encode()
