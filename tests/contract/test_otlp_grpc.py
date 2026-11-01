"""OTLP/gRPC services against a real SQLite backend (plan task P3-10.1).

REQ-003 (gRPC export), REQ-006.b, REQ-007, REQ-008, REQ-009.b; API §3.3 gRPC column. The success
path uses the official OTLP gRPC exporter; status rows use a raw stub to read codes and details.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc  # type: ignore[import-untyped]
import pytest
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from reflex_agentlens.ingest.status import RETRY_INFO_TYPE, STATUS_DETAILS_KEY, retry_info
from reflex_agentlens.store.records import ProjectPatch
from tests.contract.ingest_rig import IngestRig, ingest_rig, now_request
from tests.contract.test_otlp_http import metrics_request
from tests.unit.ingest import otlp


@pytest.fixture
async def rig(sqlite_template: Path, tmp_path: Path) -> AsyncIterator[IngestRig]:
    async with ingest_rig(sqlite_template, tmp_path, grpc=True) as r:
        yield r


def target(rig: IngestRig) -> str:
    return f"127.0.0.1:{rig.core.grpc_port}"


def request(n: int = 2, trace: int = 5) -> ExportTraceServiceRequest:
    spans = [otlp.span(trace=trace, sid=i, parent=None if i == 1 else 1) for i in range(1, n + 1)]
    return now_request(*spans)


async def export(
    rig: IngestRig, req: ExportTraceServiceRequest, *, key: str | None = "rig", **kw: Any
) -> Any:
    metadata = (
        [] if key is None else [("authorization", f"Bearer {rig.key if key == 'rig' else key}")]
    )
    async with grpc.aio.insecure_channel(target(rig)) as channel:
        stub = trace_service_pb2_grpc.TraceServiceStub(channel)
        return await stub.Export(req, metadata=metadata, timeout=5, **kw)


async def failure(
    rig: IngestRig, req: ExportTraceServiceRequest, **kw: Any
) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as err:
        await export(rig, req, **kw)
    return err.value


def retry_details(err: grpc.aio.AioRpcError) -> bytes | None:
    for key, value in err.trailing_metadata() or ():
        if key == STATUS_DETAILS_KEY:
            return bytes(value)
    return None


async def test_official_exporter_round_trip(rig: IngestRig) -> None:
    """REQ-003: the stock OTLP gRPC exporter, gzip on, key in headers."""
    exporter = OTLPSpanExporter(
        endpoint=f"http://{target(rig)}",
        insecure=True,
        headers={"authorization": f"Bearer {rig.key}"},
        compression=grpc.Compression.Gzip,
    )
    provider = TracerProvider(resource=Resource.create({"service.name": "grpc-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def emit() -> None:
        tracer = provider.get_tracer("t")
        with tracer.start_as_current_span("root"), tracer.start_as_current_span("child"):
            pass
        provider.shutdown()

    await asyncio.to_thread(emit)
    assert await rig.scalar("select count(*) from spans") == 2
    assert await rig.scalar("select service_name from sources") == "grpc-test"


async def test_partial_success(rig: IngestRig) -> None:
    good = otlp.span(trace=4, sid=1)
    bad = otlp.span(trace=4, sid=2, start_ago_s=5, end_ago_s=10)
    resp = await export(rig, now_request(good, bad), compression=grpc.Compression.Gzip)
    assert resp.partial_success.rejected_spans == 1
    assert await rig.scalar("select count(*) from spans") == 1


@pytest.mark.parametrize("key", [None, "al_bad", "al_0000000a_" + "A" * 32])
async def test_unauthenticated(rig: IngestRig, key: str | None) -> None:
    err = await failure(rig, request(), key=key)
    assert err.code() == grpc.StatusCode.UNAUTHENTICATED
    assert err.details() == "invalid api key"
    assert retry_details(err) is None


async def test_x_agentlens_key_metadata(rig: IngestRig) -> None:
    async with grpc.aio.insecure_channel(target(rig)) as channel:
        stub = trace_service_pb2_grpc.TraceServiceStub(channel)
        await stub.Export(request(), metadata=[("x-agentlens-key", rig.key)], timeout=5)
    assert await rig.scalar("select count(*) from spans") == 2


async def test_message_over_the_limit_is_resource_exhausted_without_retry_info(
    sqlite_template: Path, tmp_path: Path
) -> None:
    """REQ-007.b: non-retryable."""
    toml = "[ingest]\nmax_decompressed_bytes = 2000\n"
    async with ingest_rig(sqlite_template, tmp_path, toml, grpc=True) as r:
        big = otlp.span(trace=1, sid=1, payload="x" * 5000)
        err = await failure(r, now_request(big))
    assert err.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert retry_details(err) is None


async def test_rate_limited_has_retry_info(sqlite_template: Path, tmp_path: Path) -> None:
    """REQ-007.a: RESOURCE_EXHAUSTED + RetryInfo."""
    patch = ProjectPatch(request_rate=1, request_burst=1)
    async with ingest_rig(sqlite_template, tmp_path, patch=patch, grpc=True) as r:
        await export(r, request())
        err = await failure(r, request(trace=6))
    assert err.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    details = retry_details(err)
    assert details is not None
    assert RETRY_INFO_TYPE.encode() in details
    assert retry_info(1) in details


async def test_overload_and_storage_down_are_unavailable(rig: IngestRig) -> None:
    """REQ-009.b and REQ-330: UNAVAILABLE + RetryInfo."""
    writer = rig.rt.writer
    writer._latencies.extend([(writer.clock(), 9.0)] * 2)
    err = await failure(rig, request())
    assert err.code() == grpc.StatusCode.UNAVAILABLE
    assert retry_info(2) in (retry_details(err) or b"")
    writer._latencies.clear()
    rig.rt.queue.available = False
    err = await failure(rig, request())
    assert err.code() == grpc.StatusCode.UNAVAILABLE
    assert retry_info(5) in (retry_details(err) or b"")


async def test_invalid_argument_when_the_batch_is_rejected(rig: IngestRig) -> None:
    from reflex_agentlens.store.records import BatchRejected

    async def rejecting(batches: Any) -> list[BatchRejected]:
        return [BatchRejected("invalid_data") for _ in batches]

    rig.rt.writer.store = type("S", (), {"write_group": staticmethod(rejecting)})()
    err = await failure(rig, request())
    assert err.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert retry_details(err) is None


async def test_logs_are_unimplemented_without_auth(rig: IngestRig) -> None:
    """REQ-008.a."""
    async with grpc.aio.insecure_channel(target(rig)) as channel:
        stub = logs_service_pb2_grpc.LogsServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await stub.Export(ExportLogsServiceRequest(), timeout=5)
    assert err.value.code() == grpc.StatusCode.UNIMPLEMENTED


async def test_metrics_service(rig: IngestRig) -> None:
    """REQ-005.a over gRPC."""
    async with grpc.aio.insecure_channel(target(rig)) as channel:
        stub = metrics_service_pb2_grpc.MetricsServiceStub(channel)
        resp = await stub.Export(
            metrics_request(), metadata=[("authorization", f"Bearer {rig.key}")], timeout=5
        )
    assert resp.partial_success.rejected_data_points == 1
    assert await rig.scalar("select count(*) from metric_points") == 2


async def test_grpc_disabled_by_config(sqlite_template: Path, tmp_path: Path) -> None:
    async with ingest_rig(sqlite_template, tmp_path) as r:  # the rig sets grpc_enabled = false
        assert r.core.grpc_port is None
