"""The protocol-independent ingest core and the engine factory (plan tasks P3-09.1, P3-10.1).

One :class:`IngestCore` exists per engine run (``EngineRuntime.app``) and lives on the ingest
loop, because it owns everything bound to the store: the settings cache, the API-key cache,
rate buckets and the overload guard. OTLP/HTTP routes reach it with ``IngestEngine.call``;
the gRPC services run on the ingest loop and call it directly.

Checks follow API §3.3 (cheapest first; nothing is persisted from a failed request): size header
→ media type (HTTP layer) → :meth:`IngestCore.admit` (authentication, request bucket, overload)
→ body read (HTTP layer) → :meth:`IngestCore.decode` (decompress with caps, decode) → span bucket
→ per-span validation → enqueue → commit → respond.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsPartialSuccess,
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from sqlalchemy.exc import InterfaceError, OperationalError

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import NotFound, StorageUnavailable
from reflex_agentlens.ingest.admission import Authenticator, KeyStore, Principal
from reflex_agentlens.ingest.decode import Encoding, decode_metrics, decode_traces
from reflex_agentlens.ingest.engine import EngineRuntime, EngineStore, IngestEngine, Service
from reflex_agentlens.ingest.errors import BatchRejectedError, Unauthenticated
from reflex_agentlens.ingest.jobs import JobStore, SettingsCache, job_services
from reflex_agentlens.ingest.limits import OverloadGuard, RateLimiter, decode_content
from reflex_agentlens.ingest.metrics import genai_points, normalize_metrics, scrubber
from reflex_agentlens.ingest.normalize.pipeline import normalize_batch
from reflex_agentlens.ingest.redaction.hook import load_hook
from reflex_agentlens.store.records import BatchRejected, IngestSettings, MetricPoint

__all__ = ["Admitted", "CoreStore", "IngestCore", "Signal", "build_engine"]

log = logging.getLogger(__name__)

Signal = Literal["traces", "metrics"]
ExportRequest = ExportTraceServiceRequest | ExportMetricsServiceRequest
ExportResponse = ExportTraceServiceResponse | ExportMetricsServiceResponse


class CoreStore(JobStore, KeyStore, Protocol):
    async def write_metric_points(self, points: list[MetricPoint]) -> int: ...


@dataclass(frozen=True, slots=True)
class Admitted:
    principal: Principal
    settings: IngestSettings

    @property
    def project_id(self) -> str:
        return self.principal.project_id


class IngestCore:
    def __init__(
        self,
        rt: EngineRuntime,
        cfg: AgentlensConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.rt = rt
        self.store = cast(CoreStore, rt.store)
        ingest = cfg.ingest
        hook = load_hook(ingest.redaction_hook) if ingest.redaction_hook else None
        self.cache = SettingsCache(self.store, cfg, hook=hook)
        self.auth = Authenticator(self.store, clock=clock, wall_ns=clock_ns)
        self.limiter = RateLimiter(clock)
        self.guard = OverloadGuard(
            rt.writer,
            commit_latency_s=ingest.overload_commit_latency.total_seconds(),
            retry_after_s=ingest.overload_retry_after.total_seconds(),
        )
        self.max_decompressed_bytes = ingest.max_decompressed_bytes
        self.store_metrics = ingest.store_metrics
        self.clock_ns = clock_ns
        self.metrics_discarded = 0  # REQ-005.b counter
        self.grpc_port: int | None = None  # the bound port, once the gRPC service is up

    # ── checks before the body ───────────────────────────────────────────────────────────────

    async def admit(self, headers: Mapping[str, str]) -> Admitted:
        """Authentication, then the request bucket, then overload (API §3.3 order)."""
        principal = await self.auth.authenticate(headers)
        try:
            snap = await self.cache.project(principal.project_id)
        except NotFound:  # the Project was deleted while the key was cached
            raise Unauthenticated("unknown") from None
        settings = snap.settings
        self.limiter.check_request(
            principal.project_id, settings.request_rate, settings.request_burst
        )
        self.guard.check()
        return Admitted(principal, settings)

    # ── body ─────────────────────────────────────────────────────────────────────────────────

    def decode(
        self, signal: Signal, body: bytes, content_encoding: str | None, encoding: Encoding
    ) -> ExportRequest:
        data = decode_content(body, content_encoding, self.max_decompressed_bytes)
        if signal == "traces":
            return decode_traces(data, encoding)
        return decode_metrics(data, encoding)

    async def export_http(
        self,
        signal: Signal,
        admitted: Admitted,
        body: bytes,
        content_encoding: str | None,
        encoding: Encoding,
    ) -> ExportResponse:
        request = self.decode(signal, body, content_encoding, encoding)
        if isinstance(request, ExportTraceServiceRequest):
            return await self.export_traces(admitted, request)
        return await self.export_metrics(admitted, request)

    # ── signals ──────────────────────────────────────────────────────────────────────────────

    async def export_traces(
        self, admitted: Admitted, request: ExportTraceServiceRequest
    ) -> ExportTraceServiceResponse:
        pid, settings = admitted.project_id, admitted.settings
        spans = sum(len(ss.spans) for rs in request.resource_spans for ss in rs.scope_spans)
        self.limiter.check_spans(pid, spans, settings.span_rate, settings.span_burst)
        try:
            ctx = await self.cache.context(pid, self.clock_ns())
        except NotFound:
            raise Unauthenticated("unknown") from None
        result = normalize_batch(request, ctx)
        if result.batch.spans:
            # shielded: a client that disconnects must not cancel an accepted batch's commit
            outcome = await asyncio.shield(self.rt.writer.queue.put(result.batch))
            if isinstance(outcome, BatchRejected):
                log.warning("batch rejected: %s", outcome.reason)
                raise BatchRejectedError()
        response = ExportTraceServiceResponse()
        if result.rejections.total:
            response.partial_success.CopyFrom(
                ExportTracePartialSuccess(
                    rejected_spans=result.rejections.total,
                    error_message=result.rejections.message(),
                )
            )
        return response

    async def export_metrics(
        self, admitted: Admitted, request: ExportMetricsServiceRequest
    ) -> ExportMetricsServiceResponse:
        pid, settings = admitted.project_id, admitted.settings
        # data points count against the span bucket, like spans (REQ-007.a)
        self.limiter.check_spans(
            pid, genai_points(request), settings.span_rate, settings.span_burst
        )
        try:
            ctx = await self.cache.context(pid, self.clock_ns())
        except NotFound:
            raise Unauthenticated("unknown") from None
        result = normalize_metrics(request, pid, scrubber(ctx))
        self.metrics_discarded += result.discarded
        if result.points and self.store_metrics:
            try:
                await self.store.write_metric_points(result.points)
            except NotFound:
                raise Unauthenticated("unknown") from None
            except (OSError, OperationalError, InterfaceError) as exc:
                log.error("metric write failed: %s", type(exc).__name__)
                raise StorageUnavailable("commit failed; retry later") from exc
        response = ExportMetricsServiceResponse()
        if result.rejections.total:
            response.partial_success.CopyFrom(
                ExportMetricsPartialSuccess(
                    rejected_data_points=result.rejections.total,
                    error_message=result.rejections.message(),
                )
            )
        return response


# ── engine factory ───────────────────────────────────────────────────────────────────────────


def _core(rt: EngineRuntime) -> IngestCore:
    return cast(IngestCore, rt.app)


def build_engine(
    cfg: AgentlensConfig,
    open_store: Callable[[], Awaitable[EngineStore]],
    *,
    extra_services: list[Service] | None = None,
) -> IngestEngine:
    """The ingest engine of ``serve`` / ``collect``: core, jobs, caches and (optionally) gRPC."""
    from reflex_agentlens.ingest.grpc import grpc_service

    async def auth_watch(rt: EngineRuntime) -> None:
        await _core(rt).auth.watch()

    services: list[Service] = [*job_services(cfg, lambda rt: _core(rt).cache), auth_watch]
    if cfg.server.grpc_enabled:
        services.append(grpc_service(cfg))
    services.extend(extra_services or [])
    return IngestEngine(
        open_store,
        queue_memory_bytes=cfg.ingest.queue_memory_bytes,
        shutdown_grace_s=cfg.server.shutdown_grace.total_seconds(),
        services=services,
        setup=lambda rt: IngestCore(rt, cfg),
    )
