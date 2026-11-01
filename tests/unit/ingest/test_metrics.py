"""GenAI metrics normalization (plan task P3-11.1; REQ-005.a/b, Amendment A2)."""

from __future__ import annotations

from typing import Any

from google.protobuf import json_format  # type: ignore[import-untyped]
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from reflex_agentlens.ingest.cost import PricingSnapshot
from reflex_agentlens.ingest.metrics import genai_points, normalize_metrics, scrubber
from reflex_agentlens.ingest.normalize.pipeline import BatchContext
from reflex_agentlens.ingest.redaction.rules import compile_rules
from reflex_agentlens.store.records import RedactionRuleSet

T = "1793000000000000000"


def req(*metrics: dict[str, Any], service: str = "svc") -> ExportMetricsServiceRequest:
    doc = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeMetrics": [{"metrics": list(metrics)}],
            }
        ]
    }
    return json_format.ParseDict(doc, ExportMetricsServiceRequest())


def attr(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def test_counter_gauge_and_histogram_fields() -> None:
    result = normalize_metrics(
        req(
            {
                "name": "gen_ai.client.inference.usage.cached_input",
                "unit": "{token}",
                "sum": {
                    "isMonotonic": True,
                    "aggregationTemporality": 1,
                    "dataPoints": [
                        {
                            "timeUnixNano": T,
                            "asInt": "5",
                            "attributes": [
                                attr("gen_ai.request.model", "gpt-4o"),
                                attr("gen_ai.response.model", "gpt-4o-2024"),
                            ],
                        }
                    ],
                },
            },
            {
                "name": "gen_ai.server.queue_size",
                "gauge": {"dataPoints": [{"timeUnixNano": T, "asDouble": 2.5}]},
            },
            {
                "name": "gen_ai.client.operation.duration",
                "histogram": {
                    "aggregationTemporality": 2,
                    "dataPoints": [
                        {
                            "startTimeUnixNano": "1",
                            "timeUnixNano": T,
                            "count": "3",
                            "sum": 1.5,
                            "bucketCounts": ["1", "2"],
                            "explicitBounds": [1.0],
                        }
                    ],
                },
            },
        ),
        "p",
    )
    counter, gauge, hist = result.points
    assert (counter.kind, counter.value_int, counter.is_monotonic, counter.temporality) == (
        "sum",
        5,
        True,
        1,
    )
    assert (counter.model, counter.token_type, counter.unit) == (
        "gpt-4o-2024",
        "cached_input",
        "{token}",
    )
    assert counter.start_ns == counter.time_ns  # no start time: the point time
    assert counter.source.service_name == "svc"
    assert (gauge.kind, gauge.value_double, gauge.token_type) == ("gauge", 2.5, None)
    assert (hist.count, hist.sum, hist.bucket_counts, hist.explicit_bounds) == (
        3,
        1.5,
        [1, 2],
        [1.0],
    )
    assert hist.start_ns == 1
    assert result.rejections.total == 0


def test_legacy_token_type_comes_from_the_attribute() -> None:
    result = normalize_metrics(
        req(
            {
                "name": "gen_ai.client.token.usage",
                "histogram": {
                    "dataPoints": [
                        {
                            "timeUnixNano": T,
                            "count": "1",
                            "attributes": [attr("gen_ai.token.type", "output")],
                        }
                    ]
                },
            }
        ),
        "p",
    )
    assert result.points[0].token_type == "output"  # noqa: S105 - not a secret


def test_non_genai_metrics_are_discarded_not_rejected() -> None:
    """REQ-005.b."""
    result = normalize_metrics(
        req(
            {
                "name": "http.server.duration",
                "gauge": {"dataPoints": [{"timeUnixNano": T, "asDouble": 1}] * 3},
            },
            {"name": "process.cpu", "sum": {"dataPoints": [{"timeUnixNano": T, "asInt": "1"}]}},
        ),
        "p",
    )
    assert result.points == []
    assert result.discarded == 4
    assert result.rejections.total == 0


def test_invalid_genai_points_are_rejected() -> None:
    result = normalize_metrics(
        req(
            {"name": "gen_ai.a", "gauge": {"dataPoints": [{"asInt": "1"}]}},  # no timestamp
            {"name": "gen_ai.b", "gauge": {"dataPoints": [{"timeUnixNano": T}]}},  # no value
            {"name": "gen_ai.c", "gauge": {"dataPoints": [{"timeUnixNano": T, "asDouble": "NaN"}]}},
            {
                "name": "gen_ai.d",
                "summary": {"dataPoints": [{"timeUnixNano": T}, {"timeUnixNano": T}]},
            },
            {
                "name": "gen_ai." + "x" * 200,
                "gauge": {"dataPoints": [{"timeUnixNano": T, "asInt": "1"}]},
            },
            {"name": "gen_ai.e", "histogram": {"dataPoints": [{"count": "1"}]}},
            {
                "name": "gen_ai.f",
                "histogram": {
                    "dataPoints": [
                        {"timeUnixNano": T, "count": "1", "explicitBounds": ["Infinity"]}
                    ]
                },
            },
        ),
        "p",
    )
    assert result.points == []
    assert result.rejections.counts() == {
        "missing_timestamp": 2,
        "missing_value": 1,
        "non_finite_value": 2,
        "unsupported_type": 2,
        "name_too_long": 1,
    }
    assert result.rejections.message().startswith("8 data points rejected")


# ── same treatment as span attributes (security review: REQ-039/041 parity) ─────────────────


def ctx_with(**kw: Any) -> BatchContext:
    rules = compile_rules(
        RedactionRuleSet({"email": True}, [], [{"key_glob": "user.token", "action": "drop"}])
    )
    base: dict[str, Any] = {
        "project_id": "p",
        "received_at_ns": 1,
        "rules": rules,
        "session_secret": b"k" * 32,
        "pricing": PricingSnapshot(()),
        "max_attribute_value_bytes": 64,
    }
    return BatchContext(**(base | kw))


def gauge_with(*attrs: dict[str, Any]) -> ExportMetricsServiceRequest:
    point = {"timeUnixNano": T, "asInt": "1", "attributes": list(attrs)}
    return req({"name": "gen_ai.custom", "gauge": {"dataPoints": [point]}})


def test_point_attributes_are_redacted_and_truncated() -> None:
    request = gauge_with(
        attr("user.email", "ana@example.com"),
        attr("user.token", "s3cret"),
        attr("note", "y" * 500),
    )
    (point,) = normalize_metrics(request, "p", scrubber(ctx_with())).points
    assert "ana@example.com" not in str(point.attributes)
    assert "user.token" not in point.attributes
    note = point.attributes["note"]
    assert isinstance(note, str)
    assert len(note.encode()) < 200
    assert "truncated" in note
    raw = normalize_metrics(request, "p").points[0]
    assert point.attrs_hash != raw.attrs_hash  # hashed after scrubbing


def test_hook_failure_rejects_the_point() -> None:
    def boom(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("no")

    result = normalize_metrics(gauge_with(attr("a", "b")), "p", scrubber(ctx_with(hook=boom)))
    assert result.points == []
    assert result.rejections.counts() == {"redaction_hook_error": 1}


def test_genai_points_counts_only_genai() -> None:
    request = req(
        {"name": "gen_ai.x", "gauge": {"dataPoints": [{"timeUnixNano": T, "asInt": "1"}] * 3}},
        {"name": "other", "gauge": {"dataPoints": [{"timeUnixNano": T, "asInt": "1"}] * 5}},
    )
    assert genai_points(request) == 3
