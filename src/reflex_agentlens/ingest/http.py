"""OTLP/HTTP routes (plan task P3-09.1; REQ-001, 002, 005.a, 007, 008, 009.b, 010; API §3).

Starlette routes: ``POST {prefix}/v1/traces`` and ``{prefix}/v1/metrics`` ingest; ``/v1/logs``
answers 404 without an auth check so exporters stop retrying (REQ-008). Protobuf and JSON are
accepted and the response uses the request's encoding (REQ-002.b). Failures are a
``google.rpc.Status`` with a fixed message and the HTTP status of API §3.3; 429 and 503 carry
``Retry-After``.

The handler runs on the web server's loop and only does what needs the request object (headers,
streaming the body); everything else runs on the ingest loop through ``IngestEngine.call``.
"""

from __future__ import annotations

import logging
import math
from typing import cast

from google.protobuf import json_format  # type: ignore[import-untyped]
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.ingest.core import ExportResponse, IngestCore, Signal
from reflex_agentlens.ingest.decode import Encoding
from reflex_agentlens.ingest.engine import EngineRuntime, IngestEngine
from reflex_agentlens.ingest.errors import IngestFailure, UnsupportedMediaType
from reflex_agentlens.ingest.limits import check_content_length, read_limited
from reflex_agentlens.ingest.status import status_json, status_proto

__all__ = ["CONTENT_TYPES", "GRPC_CODES", "otlp_routes"]

log = logging.getLogger(__name__)

CONTENT_TYPES: dict[str, Encoding] = {
    "application/x-protobuf": "protobuf",
    "application/json": "json",
}
_MEDIA: dict[Encoding, str] = {"protobuf": "application/x-protobuf", "json": "application/json"}
_ENCODINGS = {"", "identity", "gzip"}
STORAGE_RETRY_AFTER_S = 5.0

# google.rpc.Code for each HTTP status of API §3.3
GRPC_CODES = {400: 3, 401: 16, 404: 12, 413: 8, 415: 3, 429: 8, 500: 13, 503: 14}


def media_type(content_type: str | None) -> Encoding:
    """``Content-Type`` without parameters; 415 for anything but protobuf or JSON."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    encoding = CONTENT_TYPES.get(base)
    if encoding is None:
        raise UnsupportedMediaType()
    return encoding


def _status_response(
    http_status: int, message: str, encoding: Encoding, retry_after_s: float | None = None
) -> Response:
    code = GRPC_CODES.get(http_status, 2)
    body = status_json(code, message) if encoding == "json" else status_proto(code, message)
    headers = {}
    if retry_after_s is not None:
        headers["Retry-After"] = str(max(1, math.ceil(retry_after_s)))
    return Response(body, http_status, headers=headers, media_type=_MEDIA[encoding])


def _ok(message: ExportResponse, encoding: Encoding) -> Response:
    if encoding == "json":
        body = json_format.MessageToJson(message, indent=None).encode()
    else:
        body = message.SerializeToString()
    return Response(body, 200, media_type=_MEDIA[encoding])


def otlp_routes(engine: IngestEngine, cfg: AgentlensConfig, prefix: str = "") -> list[Route]:
    max_body = cfg.ingest.max_body_bytes

    def core(rt: EngineRuntime) -> IngestCore:
        return cast(IngestCore, rt.app)

    async def export(request: Request, signal: Signal) -> Response:
        encoding: Encoding = "protobuf"  # error bodies before the media type is known
        try:
            check_content_length(request.headers, max_body)
            encoding = media_type(request.headers.get("content-type"))
            content_encoding = request.headers.get("content-encoding", "").strip().lower()
            if content_encoding not in _ENCODINGS:
                raise UnsupportedMediaType()
            headers = dict(request.headers)
            admitted = await engine.call(lambda rt: core(rt).admit(headers))
            body = await read_limited(request.stream(), max_body)
            result = await engine.call(
                lambda rt: core(rt).export_http(signal, admitted, body, content_encoding, encoding)
            )
            return _ok(result, encoding)
        except IngestFailure as exc:
            if exc.http_status >= 500:
                log.warning("ingest refused: %s", type(exc).__name__)
            return _status_response(exc.http_status, exc.message, encoding, exc.retry_after_s)
        except StorageUnavailable:
            return _status_response(503, "storage unavailable", encoding, STORAGE_RETRY_AFTER_S)
        except Exception:
            log.exception("ingest request failed")
            return _status_response(500, "internal error", encoding)

    async def traces(request: Request) -> Response:
        return await export(request, "traces")

    async def metrics(request: Request) -> Response:
        return await export(request, "metrics")

    async def logs(request: Request) -> Response:
        """REQ-008.a: unimplemented signal, no authentication."""
        try:
            encoding = media_type(request.headers.get("content-type"))
        except UnsupportedMediaType:
            encoding = "protobuf"
        return _status_response(404, "logs are not supported", encoding)

    return [
        Route(f"{prefix}/v1/traces", traces, methods=["POST"]),
        Route(f"{prefix}/v1/metrics", metrics, methods=["POST"]),
        Route(f"{prefix}/v1/logs", logs, methods=["POST"]),
    ]
