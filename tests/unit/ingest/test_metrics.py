"""GenAI metrics normalization (plan task P3-11.1; REQ-005.a/b, Amendment A2)."""

from __future__ import annotations

from typing import Any

from google.protobuf import json_format  # type: ignore[import-untyped]
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from reflex_agentlens.ingest.metrics import normalize_metrics

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
