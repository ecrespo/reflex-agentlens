"""Decode and validate (plan task P3-02.1; REQ-001.c, REQ-002.a, REQ-010.a/b, API §3.6)."""

from __future__ import annotations

import json

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from reflex_agentlens.ingest.decode import decode_metrics, decode_traces
from reflex_agentlens.ingest.errors import DecodeError
from reflex_agentlens.ingest.validate import Rejections, ValidationLimits, validate_span
from tests.unit.ingest.otlp import NOW_NS, S, request, span, to_otlp_json

DAY = 86_400 * S


def test_protobuf_and_json_decode_to_the_same_request() -> None:
    """REQ-001 / REQ-002.a: both encodings carry the same spans (hex IDs in JSON)."""
    req = request(span(1, 1, None, gen_ai__operation__name="chat"), span(1, 2, 1, tokens=3))
    from_pb = decode_traces(req.SerializeToString(), "protobuf")
    from_json = decode_traces(to_otlp_json(req), "json")
    assert from_pb == req
    assert from_json == req
    assert from_json.resource_spans[0].scope_spans[0].spans[1].parent_span_id == (1).to_bytes(
        8, "big"
    )


def test_json_accepts_snake_case_and_unknown_fields() -> None:
    doc = {
        "resource_spans": [
            {
                "scope_spans": [
                    {
                        "spans": [
                            {
                                "trace_id": "0" * 31 + "1",
                                "span_id": "0" * 15 + "2",
                                "name": "x",
                                "futureField": 1,
                            }
                        ]
                    }
                ]
            }
        ]
    }
    req = decode_traces(json.dumps(doc).encode(), "json")
    sp = req.resource_spans[0].scope_spans[0].spans[0]
    assert (sp.trace_id[-1], sp.span_id[-1], sp.name) == (1, 2, "x")


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        (b"\xff\xff\xff", "protobuf"),
        (b"not json", "json"),
        (b"[1, 2]", "json"),
        (b'{"resourceSpans": {"x": 1}}', "json"),
        (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "zz"}]}]}]}', "json"),
        (b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"name": 5}]}]}]}', "json"),
        (b"\xc3\x28", "json"),
        (b"[" * 100_000, "json"),
    ],
)
def test_undecodable_payloads_raise_decode_error(body: bytes, encoding: str) -> None:
    """REQ-001.c: 400 with a fixed message, never the input."""
    with pytest.raises(DecodeError) as err:
        decode_traces(body, encoding)  # type: ignore[arg-type]
    assert err.value.http_status == 400
    assert str(err.value) == "invalid payload"


def test_metrics_decode_with_hex_exemplar_ids() -> None:
    req = ExportMetricsServiceRequest()
    metric = req.resource_metrics.add().scope_metrics.add().metrics.add()
    metric.name = "gen_ai.client.token.usage"
    point = metric.histogram.data_points.add()
    point.count = 1
    ex = point.exemplars.add()
    ex.trace_id = (7).to_bytes(16, "big")
    ex.span_id = (8).to_bytes(8, "big")
    assert decode_metrics(req.SerializeToString(), "protobuf") == req
    from google.protobuf import json_format  # type: ignore[import-untyped]

    doc = json_format.MessageToDict(req)
    ex_doc = doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"]["dataPoints"][
        0
    ]["exemplars"][0]
    ex_doc["traceId"], ex_doc["spanId"] = (
        (7).to_bytes(16, "big").hex(),
        (8).to_bytes(8, "big").hex(),
    )
    assert decode_metrics(json.dumps(doc).encode(), "json") == req
    with pytest.raises(DecodeError):
        decode_metrics(b'{"resourceMetrics": [{"scopeMetrics": [{"metrics": [1]}]}]}', "json")


# ── validation (API §3.6) ────────────────────────────────────────────────────────────────────

LIMITS = ValidationLimits(received_at_ns=NOW_NS, retention_max_age_s=30 * 86_400)


def raw(**kw: object) -> Span:
    base = span()
    for key, value in kw.items():
        setattr(base, key, value)
    return base


@pytest.mark.parametrize(
    ("sp", "reason"),
    [
        (raw(), None),
        (raw(trace_id=b"\x01" * 8), "invalid_trace_id"),
        (raw(trace_id=bytes(16)), "invalid_trace_id"),
        (raw(span_id=b"\x01" * 4), "invalid_span_id"),
        (raw(span_id=bytes(8)), "invalid_span_id"),
        (raw(parent_span_id=b"\x01" * 3), "invalid_parent_span_id"),
        (raw(start_time_unix_nano=0), "missing_start_time"),
        (raw(end_time_unix_nano=0), "end_before_start"),
        (raw(end_time_unix_nano=NOW_NS - 20 * S), "end_before_start"),
        (raw(start_time_unix_nano=NOW_NS + 2 * 3600 * S, end_time_unix_nano=NOW_NS + 7201 * S),
         "future_timestamp"),
        (raw(start_time_unix_nano=NOW_NS + 3000 * S, end_time_unix_nano=NOW_NS + 3001 * S), None),
        (raw(start_time_unix_nano=NOW_NS - 31 * DAY, end_time_unix_nano=NOW_NS - 31 * DAY + S),
         "older_than_retention"),
        (raw(start_time_unix_nano=NOW_NS - 29 * DAY, end_time_unix_nano=NOW_NS - 29 * DAY), None),
    ],
)  # fmt: skip
def test_validation_rules(sp: Span, reason: str | None) -> None:
    """REQ-010.a: each API §3.6 rule; A-17 clock-skew rules."""
    assert validate_span(sp, LIMITS) == reason


def test_retention_forever_and_huge_ages() -> None:
    old = raw(start_time_unix_nano=1, end_time_unix_nano=2)
    assert validate_span(old, ValidationLimits(NOW_NS, retention_max_age_s=0)) is None
    assert validate_span(old, ValidationLimits(NOW_NS, retention_max_age_s=2**62)) is None


def test_partial_success_message() -> None:
    """REQ-010.b: counts per reason, stable order, no content."""
    rej = Rejections()
    assert (rej.total, rej.message()) == (0, "")
    rej.add("end_before_start")
    rej.add("invalid_span_id", 2)
    assert rej.total == 3
    assert rej.message() == "3 spans rejected: invalid_span_id=2, end_before_start=1"
    points = Rejections("data points")
    points.add("b")
    points.add("a")
    assert points.message() == "2 data points rejected: a=1, b=1"
    assert points.counts() == {"a": 1, "b": 1}
