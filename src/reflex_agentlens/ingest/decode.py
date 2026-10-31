"""OTLP request decoding (plan task P3-02.1; REQ-001.c, REQ-002.a, API §3.2).

Protobuf goes through the upb runtime. OTLP/JSON differs from the protobuf JSON mapping in one
way that matters: trace and span IDs are **hex** strings, not base64. :func:`decode_traces`
rewrites those fields to base64 before ``json_format`` parses the document, so both encodings
produce the same message. Any failure raises :class:`DecodeError` (400) without echoing input.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from typing import Any, Literal, cast

from google.protobuf import json_format  # type: ignore[import-untyped]
from google.protobuf.message import (  # type: ignore[import-untyped]
    DecodeError as ProtobufDecodeError,
)
from google.protobuf.message import Message
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from reflex_agentlens.ingest.errors import DecodeError

__all__ = ["Encoding", "decode_metrics", "decode_traces"]

Encoding = Literal["protobuf", "json"]
_ID_FIELDS = ("traceId", "spanId", "parentSpanId", "trace_id", "span_id", "parent_span_id")


def _hex_to_b64(value: Any) -> Any:
    if not isinstance(value, str) or value == "":
        return value
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise DecodeError() from None
    return base64.b64encode(raw).decode()


def _fix_ids(node: dict[str, Any]) -> None:
    for name in _ID_FIELDS:
        if name in node:
            node[name] = _hex_to_b64(node[name])


def _walk_traces(doc: Any) -> None:
    for rs in _items(doc, "resourceSpans", "resource_spans"):
        for ss in _items(rs, "scopeSpans", "scope_spans"):
            for span in _items(ss, "spans"):
                _fix_ids(span)
                for link in _items(span, "links"):
                    _fix_ids(link)


def _walk_metrics(doc: Any) -> None:
    """Exemplars carry trace and span IDs in hex as well."""
    for rm in _items(doc, "resourceMetrics", "resource_metrics"):
        for sm in _items(rm, "scopeMetrics", "scope_metrics"):
            for metric in _items(sm, "metrics"):
                if not isinstance(metric, dict):
                    raise DecodeError()
                for data in metric.values():
                    for point in _items(data, "dataPoints", "data_points"):
                        for exemplar in _items(point, "exemplars"):
                            _fix_ids(exemplar)


def _items(node: Any, *names: str) -> list[Any]:
    if not isinstance(node, dict):
        return []
    for name in names:
        value = node.get(name)
        if value is not None:
            if not isinstance(value, list):
                raise DecodeError()
            return value
    return []


def _decode(
    body: bytes, encoding: Encoding, message: type[Message], walk: Callable[[Any], None]
) -> Any:
    out = message()
    try:
        if encoding == "protobuf":
            out.ParseFromString(body)
            return out
        doc = json.loads(body)
        if not isinstance(doc, dict):
            raise DecodeError()
        walk(doc)
        json_format.ParseDict(doc, out, ignore_unknown_fields=True)
    except DecodeError:
        raise
    except (
        ProtobufDecodeError,
        json_format.ParseError,
        ValueError,  # includes UnicodeDecodeError and json.JSONDecodeError
        RecursionError,
        binascii.Error,
        TypeError,
    ):
        raise DecodeError() from None
    return out


def decode_traces(body: bytes, encoding: Encoding) -> ExportTraceServiceRequest:
    return cast(
        ExportTraceServiceRequest, _decode(body, encoding, ExportTraceServiceRequest, _walk_traces)
    )


def decode_metrics(body: bytes, encoding: Encoding) -> ExportMetricsServiceRequest:
    return cast(
        ExportMetricsServiceRequest,
        _decode(body, encoding, ExportMetricsServiceRequest, _walk_metrics),
    )
