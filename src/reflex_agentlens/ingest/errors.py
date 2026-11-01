"""Ingest request failures and their protocol status (API §3.3; Tech Design §5.5).

Each failure carries the fixed ``google.rpc.Status`` message of API §3.3 and its HTTP status; the
gRPC code and ``Retry-After`` / ``RetryInfo`` are derived in ``ingest.http`` / ``ingest.grpc``.
Messages never echo request content (REQ-323).
"""

from __future__ import annotations

from typing import Literal

from reflex_agentlens.core.errors import AgentlensError

__all__ = [
    "AuthFailure",
    "BatchRejectedError",
    "DecodeError",
    "IngestFailure",
    "Overloaded",
    "PayloadTooLarge",
    "RateLimited",
    "ShuttingDown",
    "Unauthenticated",
    "UnsupportedMediaType",
]


class IngestFailure(AgentlensError):
    """A request the receiver refuses; nothing from it is persisted."""

    http_status = 500
    message = "internal error"
    retry_after_s: float | None = None

    def __init__(self, message: str | None = None, *, retry_after_s: float | None = None) -> None:
        super().__init__(message or self.message)
        if message is not None:
            self.message = message
        if retry_after_s is not None:
            self.retry_after_s = retry_after_s


class DecodeError(IngestFailure):
    http_status = 400
    message = "invalid payload"


class BatchRejectedError(IngestFailure):
    """A batch rolled back to its savepoint (Tech Design DD-003, Analyze A-04)."""

    http_status = 400
    message = "batch rejected"


AuthFailure = Literal["missing", "malformed", "unknown", "revoked"]


class Unauthenticated(IngestFailure):
    """REQ-006.b. ``reason`` is for logs and metrics only; the client sees the fixed message."""

    http_status = 401
    message = "invalid api key"

    def __init__(self, reason: AuthFailure = "unknown") -> None:
        super().__init__()
        self.reason: AuthFailure = reason


class PayloadTooLarge(IngestFailure):
    http_status = 413
    message = "payload too large"


class UnsupportedMediaType(IngestFailure):
    http_status = 415
    message = "unsupported media type"


class RateLimited(IngestFailure):
    http_status = 429
    message = "rate limit exceeded"


class Overloaded(IngestFailure):
    http_status = 503
    message = "overloaded"


class ShuttingDown(IngestFailure):
    http_status = 503
    message = "shutting down"
    retry_after_s = 1.0
