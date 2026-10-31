"""Spike S1c: ingest thread with grpc.aio next to the Reflex backend (Tasks P0-04.1, P0-04.2).

DD-001 shape: a dedicated thread owns its own asyncio loop and runs the OTLP gRPC server; the
OTLP/HTTP route lives on the Reflex (Starlette) backend and hands raw bytes to the ingest loop with
``asyncio.run_coroutine_threadsafe``. Started and stopped by a Reflex lifespan context manager, so
it runs inside the Granian worker process (Granian's Server spawns the worker as a child process,
even with one worker). Counting only: parse ``ExportTraceServiceRequest`` and count spans.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time

import grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

log = logging.getLogger("agentlens.ingest_spike")
GRPC_PORT = int(os.environ.get("AGENTLENS_SPIKE_GRPC_PORT", "4317"))


def count_spans(req: trace_service_pb2.ExportTraceServiceRequest) -> int:
    return sum(len(ss.spans) for rs in req.resource_spans for ss in rs.scope_spans)


class _TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, owner: IngestThread) -> None:
        self.owner = owner

    async def Export(self, request, context):  # noqa: N802 - gRPC method name
        self.owner.grpc_spans += count_spans(request)
        self.owner.grpc_requests += 1
        return trace_service_pb2.ExportTraceServiceResponse()


class IngestThread:
    def __init__(self, grpc_port: int = GRPC_PORT) -> None:
        self.grpc_port = grpc_port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread = threading.Thread(target=self._run, name="agentlens-ingest", daemon=False)
        self.ready = threading.Event()
        self.http_spans = self.grpc_spans = self.http_requests = self.grpc_requests = 0
        self._inflight: set[asyncio.Future] = set()
        self._stop: asyncio.Event | None = None
        self.error: BaseException | None = None

    # --- thread side -------------------------------------------------------------------------
    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001 - surfaced to the starter
            self.error = e
            self.ready.set()
        finally:
            self.loop.close()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        server = grpc.aio.server()
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_TraceService(self), server)
        server.add_insecure_port(f"127.0.0.1:{self.grpc_port}")
        await server.start()
        self.ready.set()
        log.warning("ingest thread up: gRPC :%s", self.grpc_port)
        await self._stop.wait()
        t0 = time.monotonic()
        if self._inflight:  # drain HTTP work already handed to this loop
            await asyncio.wait(self._inflight, timeout=3)
        await server.stop(grace=1.0)
        log.warning("ingest thread drained and gRPC stopped in %.2fs", time.monotonic() - t0)

    async def handle_http(self, body: bytes) -> int:
        task = asyncio.current_task()
        self._inflight.add(task)
        try:
            req = trace_service_pb2.ExportTraceServiceRequest()
            req.ParseFromString(body)
            n = count_spans(req)
            self.http_spans += n
            self.http_requests += 1
            return n
        finally:
            self._inflight.discard(task)

    # --- caller side -------------------------------------------------------------------------
    def start(self, timeout: float = 10) -> None:
        self.thread.start()
        if not self.ready.wait(timeout) or self.error:
            raise RuntimeError(f"ingest thread failed to start: {self.error!r}")

    async def submit_http(self, body: bytes) -> int:
        fut = asyncio.run_coroutine_threadsafe(self.handle_http(body), self.loop)
        return await asyncio.wrap_future(fut)

    def stop(self, timeout: float = 5) -> None:
        if self.loop and self._stop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._stop.set)
        self.thread.join(timeout)
        log.warning("ingest thread joined: alive=%s", self.thread.is_alive())


INGEST: IngestThread | None = None


@contextlib.asynccontextmanager
async def ingest_lifespan():
    global INGEST
    INGEST = IngestThread()
    INGEST.start()
    try:
        yield
    finally:
        log.warning("lifespan shutdown: stopping ingest")
        INGEST.stop()


async def otlp_http_traces(request: Request) -> Response:
    if INGEST is None:
        return Response(status_code=503)
    if request.headers.get("content-type", "").split(";")[0] != "application/x-protobuf":
        return Response(status_code=415)
    await INGEST.submit_http(await request.body())
    return Response(
        trace_service_pb2.ExportTraceServiceResponse().SerializeToString(),
        media_type="application/x-protobuf",
    )


async def counts(_: Request) -> Response:
    i = INGEST
    return JSONResponse(
        {
            "http_spans": i.http_spans,
            "grpc_spans": i.grpc_spans,
            "http_requests": i.http_requests,
            "grpc_requests": i.grpc_requests,
            "pid": os.getpid(),
            "thread_alive": i.thread.is_alive(),
        }
        if i
        else {}
    )


ingest_api = Starlette(
    routes=[
        Route("/v1/traces", otlp_http_traces, methods=["POST"]),
        Route("/_spike/counts", counts, methods=["GET"]),
    ]
)
