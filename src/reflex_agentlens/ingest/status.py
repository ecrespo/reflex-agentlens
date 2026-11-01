"""``google.rpc.Status`` and ``RetryInfo`` without ``googleapis-common-protos`` (API §3.3).

OTLP failure bodies are a ``google.rpc.Status``; retryable gRPC failures carry a
``google.rpc.RetryInfo`` detail. Both messages are tiny and stable, so they are encoded here by
hand instead of adding a dependency:

    Status    { int32 code = 1; string message = 2; repeated google.protobuf.Any details = 3; }
    Any       { string type_url = 1; bytes value = 2; }
    RetryInfo { google.protobuf.Duration retry_delay = 1; }
    Duration  { int64 seconds = 1; int32 nanos = 2; }
"""

from __future__ import annotations

import json
import math

__all__ = [
    "RETRY_INFO_TYPE",
    "STATUS_DETAILS_KEY",
    "retry_info",
    "status_json",
    "status_proto",
]

RETRY_INFO_TYPE = "type.googleapis.com/google.rpc.RetryInfo"
STATUS_DETAILS_KEY = "grpc-status-details-bin"


def _varint(n: int) -> bytes:
    out = bytearray()
    n &= (1 << 64) - 1  # negative int32 values are encoded as 10-byte varints
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    """A length-delimited field (wire type 2)."""
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def retry_info(seconds: float) -> bytes:
    """A serialized ``RetryInfo`` with ``retry_delay`` rounded up to whole seconds."""
    whole = max(0, math.ceil(seconds))
    duration = _varint(1 << 3) + _varint(whole) if whole else b""  # Duration.seconds
    return _field(1, duration)


def status_proto(code: int, message: str, retry_after_s: float | None = None) -> bytes:
    out = b""
    if code:
        out += _varint(1 << 3) + _varint(code)
    if message:
        out += _field(2, message.encode())
    if retry_after_s is not None:
        any_msg = _field(1, RETRY_INFO_TYPE.encode()) + _field(2, retry_info(retry_after_s))
        out += _field(3, any_msg)
    return out


def status_json(code: int, message: str) -> bytes:
    """OTLP/JSON form of the same Status (details are omitted in JSON)."""
    return json.dumps({"code": code, "message": message}, separators=(",", ":")).encode()
