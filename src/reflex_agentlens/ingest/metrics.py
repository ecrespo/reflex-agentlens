"""GenAI metrics ingestion (plan task P3-11.1; REQ-005, Tech Design §5.3 Amendment A2).

Only ``gen_ai.*`` metrics are persisted. The persisted list of Tech Design §5.3 (token usage,
operation and inference durations, time to first chunk, server and agent metrics) maps to typed
columns; any other ``gen_ai.*`` metric is stored generically with the same shape. Everything
else is **discarded and counted**, which is not a rejection (REQ-005.b). A GenAI data point the
store cannot represent (no timestamp, a summary or exponential histogram, inconsistent buckets,
non-finite values) is rejected and reported in ``partialSuccess.rejectedDataPoints``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import Metric

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.ingest.normalize.spans import source_of
from reflex_agentlens.ingest.validate import Rejections
from reflex_agentlens.store.attrs import JsonValue, attributes_to_json, attrs_hash
from reflex_agentlens.store.metrics import LEGACY_TOKEN_METRIC, TOKEN_COUNTER_PREFIX
from reflex_agentlens.store.records import MetricKind, MetricPoint, SourceKey

__all__ = ["GENAI_PREFIX", "MetricsResult", "normalize_metrics"]

GENAI_PREFIX = "gen_ai."
_MAX_NAME = 128
_MAX_UNIT = 32
_TOKEN_TYPES = {"input", "output", "cached_input", "cache_write", "reasoning"}


@dataclass(slots=True)
class MetricsResult:
    points: list[MetricPoint] = field(default_factory=list)
    rejections: Rejections = field(default_factory=lambda: Rejections("data points"))
    discarded: int = 0  # non-GenAI data points (REQ-005.b counter)


def _str(attrs: dict[str, JsonValue], key: str) -> str | None:
    value = attrs.get(key)
    return value if isinstance(value, str) and value else None


def _token_type(name: str, attrs: dict[str, JsonValue]) -> str | None:
    if name.startswith(TOKEN_COUNTER_PREFIX):
        suffix = name[len(TOKEN_COUNTER_PREFIX) :]
        return suffix if suffix in _TOKEN_TYPES else None
    if name == LEGACY_TOKEN_METRIC:
        value = _str(attrs, k.GEN_AI_TOKEN_TYPE)
        return value[:16] if value else None
    return None


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def _base(
    project_id: str, source: SourceKey, metric: Metric, kind: MetricKind, dp: Any
) -> dict[str, Any] | None:
    if not dp.time_unix_nano:
        return None
    attrs = attributes_to_json(dp.attributes)
    model = _str(attrs, k.GEN_AI_RESPONSE_MODEL) or _str(attrs, k.GEN_AI_REQUEST_MODEL)
    return {
        "project_id": project_id,
        "source": source,
        "name": metric.name,
        "kind": kind,
        "start_ns": dp.start_time_unix_nano or dp.time_unix_nano,
        "time_ns": dp.time_unix_nano,
        "attrs_hash": attrs_hash(attrs),
        "attributes": attrs,
        "unit": metric.unit[:_MAX_UNIT] or None,
        "model": model[:200] if model else None,
        "token_type": _token_type(metric.name, attrs),
    }


def _number_points(
    project_id: str, source: SourceKey, metric: Metric, result: MetricsResult
) -> None:
    data = metric.sum if metric.HasField("sum") else metric.gauge
    kind: MetricKind = "sum" if metric.HasField("sum") else "gauge"
    for dp in data.data_points:
        base = _base(project_id, source, metric, kind, dp)
        which = dp.WhichOneof("value")
        if base is None or which is None:
            result.rejections.add("missing_value" if base else "missing_timestamp")
            continue
        if which == "as_double" and not _finite(dp.as_double):
            result.rejections.add("non_finite_value")
            continue
        if kind == "sum":
            base["is_monotonic"] = metric.sum.is_monotonic
            base["temporality"] = int(metric.sum.aggregation_temporality) or None
        if which == "as_int":
            base["value_int"] = dp.as_int
        else:
            base["value_double"] = dp.as_double
        result.points.append(MetricPoint(**base))


def _histogram_points(
    project_id: str, source: SourceKey, metric: Metric, result: MetricsResult
) -> None:
    for dp in metric.histogram.data_points:
        base = _base(project_id, source, metric, "histogram", dp)
        if base is None:
            result.rejections.add("missing_timestamp")
            continue
        bounds, buckets = list(dp.explicit_bounds), list(dp.bucket_counts)
        if buckets and len(buckets) != len(bounds) + 1:
            result.rejections.add("inconsistent_buckets")
            continue
        if not _finite(dp.sum, *bounds):
            result.rejections.add("non_finite_value")
            continue
        base["temporality"] = int(metric.histogram.aggregation_temporality) or None
        base["count"] = dp.count
        base["sum"] = dp.sum if dp.HasField("sum") else None
        base["bucket_counts"] = buckets or None
        base["explicit_bounds"] = bounds or None
        result.points.append(MetricPoint(**base))


def _count_points(metric: Metric) -> int:
    which = metric.WhichOneof("data")
    return len(getattr(metric, which).data_points) if which else 0


def normalize_metrics(request: ExportMetricsServiceRequest, project_id: str) -> MetricsResult:
    """Data points of the ``gen_ai.*`` metrics in the request, plus rejections and discards."""
    result = MetricsResult()
    for rm in request.resource_metrics:
        source = source_of(attributes_to_json(rm.resource.attributes))
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if not metric.name.startswith(GENAI_PREFIX):
                    result.discarded += _count_points(metric)
                    continue
                n = _count_points(metric)
                if len(metric.name) > _MAX_NAME:
                    result.rejections.add("name_too_long", n)
                    continue
                which = metric.WhichOneof("data")
                if which in ("sum", "gauge"):
                    _number_points(project_id, source, metric, result)
                elif which == "histogram":
                    _histogram_points(project_id, source, metric, result)
                elif n:  # summary / exponential histogram: not representable (Data Model §2.16)
                    result.rejections.add("unsupported_type", n)
    return result
