"""OTLP/gRPC services (plan task P3-10.1; REQ-003, 006.b, 007, 008, 009.b; API §3.1/§3.3).

``TraceService`` and ``MetricsService`` run on the ingest loop as an engine service; the server
is not started when ``[server] grpc_enabled`` is false. ``LogsService`` is not registered, so
gRPC itself answers ``UNIMPLEMENTED`` without any auth check (REQ-008.a). gzip request
compression is handled by gRPC; ``max_receive_message_length`` is the decompressed body limit.

The API key comes from the call metadata (``x-agentlens-key`` or ``authorization: Bearer``).
Failures abort with the status of API §3.3; retryable ones (429 → ``RESOURCE_EXHAUSTED``, 503 →
``UNAVAILABLE``) carry a ``google.rpc.RetryInfo`` in ``grpc-status-details-bin``, while 413 →
``RESOURCE_EXHAUSTED`` carries none (non-retryable).
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn, cast

import grpc  # type: ignore[import-untyped]
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.ingest.core import IngestCore
from reflex_agentlens.ingest.engine import EngineRuntime, Service
from reflex_agentlens.ingest.errors import IngestFailure
from reflex_agentlens.ingest.status import STATUS_DETAILS_KEY, status_proto

__all__ = ["grpc_service"]

log = logging.getLogger(__name__)

_CODES = {
    400: grpc.StatusCode.INVALID_ARGUMENT,
    401: grpc.StatusCode.UNAUTHENTICATED,
    403: grpc.StatusCode.PERMISSION_DENIED,
    413: grpc.StatusCode.RESOURCE_EXHAUSTED,
    415: grpc.StatusCode.INVALID_ARGUMENT,
    429: grpc.StatusCode.RESOURCE_EXHAUSTED,
    503: grpc.StatusCode.UNAVAILABLE,
}
_STORAGE_RETRY_S = 5.0
_STOP_GRACE_S = 1.0


def _headers(context: grpc.aio.ServicerContext[Any, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in context.invocation_metadata() or ():
        if isinstance(value, str):
            out.setdefault(key.lower(), value)
    return out


async def _abort(
    context: grpc.aio.ServicerContext[Any, Any],
    http_status: int,
    message: str,
    retry_after_s: float | None,
) -> NoReturn:
    code = _CODES.get(http_status, grpc.StatusCode.INTERNAL)
    retryable = http_status in (429, 503) and retry_after_s is not None
    if retryable:
        details = status_proto(code.value[0], message, retry_after_s)
        context.set_trailing_metadata(((STATUS_DETAILS_KEY, details),))
    await context.abort(code, message)
    raise AssertionError("unreachable")  # context.abort always raises


class _Handler:
    def __init__(self, core: IngestCore) -> None:
        self.core = core

    async def guarded(self, context: grpc.aio.ServicerContext[Any, Any], call: Any) -> Any:
        try:
            return await call()
        except IngestFailure as exc:
            await _abort(context, exc.http_status, exc.message, exc.retry_after_s)
        except StorageUnavailable:
            await _abort(context, 503, "storage unavailable", _STORAGE_RETRY_S)
        except Exception:
            log.exception("gRPC export failed")
            await _abort(context, 500, "internal error", None)


class TraceServicer(trace_service_pb2_grpc.TraceServiceServicer, _Handler):
    async def Export(
        self,
        request: ExportTraceServiceRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> ExportTraceServiceResponse:
        async def run() -> ExportTraceServiceResponse:
            admitted = await self.core.admit(_headers(context))
            return await self.core.export_traces(admitted, request)

        return cast(ExportTraceServiceResponse, await self.guarded(context, run))


class MetricsServicer(metrics_service_pb2_grpc.MetricsServiceServicer, _Handler):
    async def Export(
        self,
        request: ExportMetricsServiceRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> ExportMetricsServiceResponse:
        async def run() -> ExportMetricsServiceResponse:
            admitted = await self.core.admit(_headers(context))
            return await self.core.export_metrics(admitted, request)

        return cast(ExportMetricsServiceResponse, await self.guarded(context, run))


def grpc_service(cfg: AgentlensConfig, port: int | None = None) -> Service:
    """The gRPC server as an engine service; ``port=0`` binds an ephemeral port (tests)."""
    host = cfg.server.grpc_host or cfg.server.http_host
    bind_port = cfg.server.grpc_port if port is None else port
    max_message = cfg.ingest.max_decompressed_bytes

    async def serve(rt: EngineRuntime) -> None:
        core = cast(IngestCore, rt.app)
        server = grpc.aio.server(
            options=[
                ("grpc.max_receive_message_length", max_message),
                ("grpc.so_reuseport", 0),
            ]
        )
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(  # type: ignore[no-untyped-call]
            TraceServicer(core), server
        )
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(  # type: ignore[no-untyped-call]
            MetricsServicer(core), server
        )
        core.grpc_port = server.add_insecure_port(f"{host}:{bind_port}")
        await server.start()
        try:
            await server.wait_for_termination()
        finally:
            await server.stop(_STOP_GRACE_S)

    return serve
