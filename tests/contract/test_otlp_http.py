"""OTLP/HTTP routes against a real SQLite backend (plan task P3-09.1).

One test per row of the API §3.3 status table, plus encodings, gzip, partial success and logs.
REQ-001, REQ-002, REQ-005.a, REQ-007, REQ-008, REQ-009.b, REQ-010.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.protobuf import json_format  # type: ignore[import-untyped]
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from starlette.applications import Starlette

from reflex_agentlens.ingest.http import otlp_routes
from reflex_agentlens.ingest.status import status_proto
from reflex_agentlens.store.records import BatchRejected, ProjectPatch
from tests.contract.ingest_rig import IngestRig, ingest_rig, now_request
from tests.unit.ingest import otlp

PB = "application/x-protobuf"


@pytest.fixture
async def rig(sqlite_template: Path, tmp_path: Path) -> AsyncIterator[IngestRig]:
    async with ingest_rig(sqlite_template, tmp_path) as r:
        yield r


def client_for(rig: IngestRig) -> httpx.AsyncClient:
    app = Starlette(routes=otlp_routes(rig.engine, rig.cfg))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def traces_body(n: int = 2, trace: int = 7) -> bytes:
    spans = [otlp.span(trace=trace, sid=i, parent=None if i == 1 else 1) for i in range(1, n + 1)]
    return now_request(*spans).SerializeToString()


async def post(
    rig: IngestRig,
    body: bytes,
    *,
    path: str = "/v1/traces",
    content_type: str = PB,
    key: str | None = "rig",
    **headers: str,
) -> httpx.Response:
    hdrs = {"content-type": content_type, **{k.replace("_", "-"): v for k, v in headers.items()}}
    if key is not None:
        hdrs["authorization"] = f"Bearer {rig.key if key == 'rig' else key}"
    async with client_for(rig) as client:
        return await client.post(path, content=body, headers=hdrs)


def assert_status(resp: httpx.Response, http: int, grpc_code: int, message: str) -> None:
    assert resp.status_code == http
    assert resp.content == status_proto(grpc_code, message)


# ── success ──────────────────────────────────────────────────────────────────────────────────


async def test_protobuf_export_commits_before_200(rig: IngestRig) -> None:
    """REQ-001.a, REQ-009.a: the spans are in the database when the response arrives."""
    resp = await post(rig, traces_body(3))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == PB
    assert ExportTraceServiceResponse.FromString(resp.content) == ExportTraceServiceResponse()
    assert await rig.scalar("select count(*) from spans") == 3


async def test_json_in_json_out(rig: IngestRig) -> None:
    """REQ-002.a/b: OTLP/JSON with hex IDs; the response is JSON."""
    body = otlp.to_otlp_json(now_request(otlp.span(trace=9, sid=1)))
    resp = await post(rig, body, content_type="application/json; charset=utf-8")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {}
    assert await rig.scalar("select count(*) from spans") == 1


async def test_gzip_body(rig: IngestRig) -> None:
    """REQ-001.b."""
    resp = await post(rig, gzip.compress(traces_body(2)), content_encoding="gzip")
    assert resp.status_code == 200
    assert await rig.scalar("select count(*) from spans") == 2


async def test_x_agentlens_key_header(rig: IngestRig) -> None:
    resp = await post(rig, traces_body(1), key=None, x_agentlens_key=rig.key)
    assert resp.status_code == 200


async def test_partial_success_counts_invalid_spans(rig: IngestRig) -> None:
    """REQ-010.a: valid spans persist; the invalid one is reported."""
    good = otlp.span(trace=3, sid=1)
    bad = otlp.span(trace=3, sid=2, start_ago_s=5, end_ago_s=10)  # ends before it starts
    body = otlp.to_otlp_json(now_request(good, bad))
    resp = await post(rig, body, content_type="application/json")
    assert resp.status_code == 200
    partial = resp.json()["partialSuccess"]
    assert partial["rejectedSpans"] == "1"
    assert "end_before_start=1" in partial["errorMessage"]
    assert await rig.scalar("select count(*) from spans") == 1


# ── API §3.3 failure rows ────────────────────────────────────────────────────────────────────


async def test_undecodable_payload_is_400(rig: IngestRig) -> None:
    """REQ-001.c."""
    assert_status(await post(rig, b"\xff\xff\xff"), 400, 3, "invalid payload")
    resp = await post(rig, b"[1, 2", content_type="application/json")
    assert resp.status_code == 400
    assert json.loads(resp.content) == {"code": 3, "message": "invalid payload"}


@pytest.mark.parametrize("key", [None, "al_garbage", "al_0000000a_" + "A" * 32])
async def test_missing_or_unknown_key_is_401(rig: IngestRig, key: str | None) -> None:
    """REQ-006.b: nothing persisted."""
    assert_status(await post(rig, traces_body(), key=key), 401, 16, "invalid api key")
    assert await rig.scalar("select count(*) from spans") == 0


async def test_revoked_key_is_401(rig: IngestRig) -> None:
    await rig.store_call("revoke_api_key", rig.key_id, 1)
    await asyncio.sleep(0.05)  # settings_changed clears the verification cache
    assert_status(await post(rig, traces_body()), 401, 16, "invalid api key")


async def test_declared_size_over_the_limit_is_413(rig: IngestRig) -> None:
    """REQ-007.b: refused from the header, before the body is read."""
    resp = await post(rig, b"x" * 10, content_length=str((8 << 20) + 1))
    assert_status(resp, 413, 8, "payload too large")
    assert "retry-after" not in resp.headers


async def test_streamed_body_over_the_limit_is_413(sqlite_template: Path, tmp_path: Path) -> None:
    async with ingest_rig(sqlite_template, tmp_path, "[ingest]\nmax_body_bytes = 1000\n") as r:

        async def stream() -> AsyncIterator[bytes]:
            for _ in range(10):
                yield b"x" * 500

        app = Starlette(routes=otlp_routes(r.engine, r.cfg))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(
                "/v1/traces",
                content=stream(),
                headers={"content-type": PB, "authorization": f"Bearer {r.key}"},
            )
        assert_status(resp, 413, 8, "payload too large")


async def test_decompressed_size_over_the_limit_is_413(rig: IngestRig) -> None:
    """REQ-007.c: zip bomb."""
    deflater = zlib.compressobj(9, zlib.DEFLATED, 31)
    bomb = b"".join(deflater.compress(bytes(1 << 20)) for _ in range(64)) + deflater.flush()
    assert_status(await post(rig, bomb, content_encoding="gzip"), 413, 8, "payload too large")


@pytest.mark.parametrize(("content_type", "encoding"), [("text/plain", ""), (PB, "br"), ("", "")])
async def test_unsupported_media_type_or_encoding_is_415(
    rig: IngestRig, content_type: str, encoding: str
) -> None:
    resp = await post(rig, traces_body(), content_type=content_type, content_encoding=encoding)
    assert_status(resp, 415, 3, "unsupported media type")


async def test_project_rate_limit_is_429_with_retry_after(
    sqlite_template: Path, tmp_path: Path
) -> None:
    """REQ-007.a."""
    patch = ProjectPatch(request_rate=1, request_burst=1)
    async with ingest_rig(sqlite_template, tmp_path, patch=patch) as r:
        assert (await post(r, traces_body())).status_code == 200
        resp = await post(r, traces_body(trace=8))
        assert_status(resp, 429, 8, "rate limit exceeded")
        assert resp.headers["retry-after"] == "1"
        assert await r.scalar("select count(*) from spans") == 2


async def test_span_rate_limit_is_429(sqlite_template: Path, tmp_path: Path) -> None:
    patch = ProjectPatch(span_rate=1, span_burst=2)
    async with ingest_rig(sqlite_template, tmp_path, patch=patch) as r:
        assert (await post(r, traces_body(2))).status_code == 200
        resp = await post(r, traces_body(2, trace=8))
        assert_status(resp, 429, 8, "rate limit exceeded")
        assert int(resp.headers["retry-after"]) >= 1


async def test_overload_is_503_with_the_configured_retry_after(rig: IngestRig) -> None:
    """REQ-009.b."""
    writer = rig.rt.writer
    now = writer.clock()
    writer._latencies.extend([(now, 9.0), (now, 9.0)])
    resp = await post(rig, traces_body())
    assert_status(resp, 503, 14, "overloaded")
    assert resp.headers["retry-after"] == "2"
    assert await rig.scalar("select count(*) from spans") == 0


async def test_database_unreachable_is_503_retry_5(rig: IngestRig) -> None:
    """REQ-330."""
    rig.rt.queue.available = False
    resp = await post(rig, traces_body())
    assert_status(resp, 503, 14, "storage unavailable")
    assert resp.headers["retry-after"] == "5"


async def test_batch_rejected_by_the_database_is_400(rig: IngestRig) -> None:
    """DD-003 / A-04: not retryable."""

    async def rejecting(batches: Any) -> list[BatchRejected]:
        return [BatchRejected("constraint_violation") for _ in batches]

    rig.rt.writer.store = type("S", (), {"write_group": staticmethod(rejecting)})()
    assert_status(await post(rig, traces_body()), 400, 3, "batch rejected")


async def test_shutting_down_is_503_retry_1(sqlite_template: Path, tmp_path: Path) -> None:
    """REQ-331."""
    async with ingest_rig(sqlite_template, tmp_path) as r:
        await r.engine.stop()
        resp = await post(r, traces_body())
    assert_status(resp, 503, 14, "shutting down")
    assert resp.headers["retry-after"] == "1"


async def test_logs_are_404_without_auth(rig: IngestRig) -> None:
    """REQ-008.a."""
    resp = await post(rig, b"anything", path="/v1/logs", key=None)
    assert resp.status_code == 404


async def test_prefix(rig: IngestRig) -> None:
    app = Starlette(routes=otlp_routes(rig.engine, rig.cfg, prefix="/agentlens/otlp"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/agentlens/otlp/v1/traces",
            content=traces_body(),
            headers={"content-type": PB, "authorization": f"Bearer {rig.key}"},
        )
    assert resp.status_code == 200


# ── metrics (REQ-005) ────────────────────────────────────────────────────────────────────────


def metrics_request() -> ExportMetricsServiceRequest:
    now = otlp.NOW_NS
    doc: dict[str, Any] = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "gen_ai.client.token.usage",
                                "unit": "{token}",
                                "histogram": {
                                    "aggregationTemporality": 2,
                                    "dataPoints": [
                                        {
                                            "startTimeUnixNano": str(now - 60),
                                            "timeUnixNano": str(now),
                                            "count": "2",
                                            "sum": 30,
                                            "bucketCounts": ["1", "1", "0"],
                                            "explicitBounds": [10, 100],
                                            "attributes": [
                                                {
                                                    "key": "gen_ai.token.type",
                                                    "value": {"stringValue": "input"},
                                                }
                                            ],
                                        },
                                        {  # invalid: buckets do not match the bounds
                                            "timeUnixNano": str(now),
                                            "count": "1",
                                            "bucketCounts": ["1"],
                                            "explicitBounds": [10, 100],
                                        },
                                    ],
                                },
                            },
                            {
                                "name": "gen_ai.client.inference.usage.output",
                                "sum": {
                                    "isMonotonic": True,
                                    "aggregationTemporality": 2,
                                    "dataPoints": [{"timeUnixNano": str(now), "asInt": "42"}],
                                },
                            },
                            {
                                "name": "http.server.duration",  # discarded, not rejected
                                "gauge": {
                                    "dataPoints": [{"timeUnixNano": str(now), "asDouble": 1.5}]
                                },
                            },
                        ]
                    }
                ],
            }
        ]
    }
    return json_format.ParseDict(doc, ExportMetricsServiceRequest())


async def test_mixed_metrics_export_stores_only_genai_points(rig: IngestRig) -> None:
    """REQ-005.a/b: only GenAI points stored; partialSuccess counts only invalid GenAI points."""
    resp = await post(rig, metrics_request().SerializeToString(), path="/v1/metrics")
    assert resp.status_code == 200
    partial = ExportMetricsServiceResponse.FromString(resp.content).partial_success
    assert partial.rejected_data_points == 1
    assert "inconsistent_buckets=1" in partial.error_message
    assert await rig.scalar("select count(*) from metric_points") == 2
    assert await rig.scalar("select token_type from metric_points where kind='sum'") == "output"
    assert rig.core.metrics_discarded == 1


async def test_metrics_json(rig: IngestRig) -> None:
    body = json_format.MessageToJson(metrics_request()).encode()
    resp = await post(rig, body, path="/v1/metrics", content_type="application/json")
    assert resp.status_code == 200
    assert resp.json()["partialSuccess"]["rejectedDataPoints"] == "1"


async def test_metric_points_count_against_the_span_bucket(
    sqlite_template: Path, tmp_path: Path
) -> None:
    """Security review: metrics are rate-limited by volume, not only per request."""
    patch = ProjectPatch(span_rate=1, span_burst=2)
    async with ingest_rig(sqlite_template, tmp_path, patch=patch) as r:
        body = metrics_request().SerializeToString()  # 3 GenAI points > burst 2, full bucket
        assert (await post(r, body, path="/v1/metrics")).status_code == 200
        resp = await post(r, body, path="/v1/metrics")
        assert_status(resp, 429, 8, "rate limit exceeded")
