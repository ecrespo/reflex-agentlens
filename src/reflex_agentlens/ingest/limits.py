"""Ingest limits: rate buckets, body caps and overload (plan task P3-08.2).

REQ-007.a-c, REQ-009.b, Tech Design DD-015, API §3.3/§3.5.

- **Token buckets** per (process, Project) for requests and spans, refilled lazily. A denied take
  raises :class:`RateLimited` with ``Retry-After`` = time until enough tokens exist, rounded up,
  at least 1 s. A batch larger than the burst is admitted only from a full bucket, which then goes
  into debt, so an oversized batch is slowed down rather than refused forever.
- **Body caps**: ``Content-Length`` is checked before reading; the stream is read up to the cap
  and abandoned beyond it (REQ-007.b). gzip is inflated incrementally with an output cap, so a
  zip bomb stops at the cap without ever allocating the inflated size (REQ-007.c).
- **Overload**: queue at its byte budget, or commit latency p50 over the threshold → 503 with
  ``overload_retry_after``; storage down → ``StorageUnavailable`` (REQ-009.b, REQ-330).
"""

from __future__ import annotations

import math
import time
import zlib
from collections.abc import AsyncIterable, Callable, Mapping
from dataclasses import dataclass

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.ingest.errors import (
    DecodeError,
    Overloaded,
    PayloadTooLarge,
    RateLimited,
    UnsupportedMediaType,
)
from reflex_agentlens.ingest.writer import Writer

__all__ = [
    "OverloadGuard",
    "RateLimiter",
    "TokenBucket",
    "check_content_length",
    "decode_content",
    "inflate_gzip",
    "read_limited",
]

_CHUNK = 64 << 10


# ── token buckets (DD-015) ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TokenBucket:
    rate: float  # tokens per second
    burst: float
    tokens: float
    updated: float

    @classmethod
    def full(cls, rate: float, burst: float, now: float) -> TokenBucket:
        return cls(rate, burst, burst, now)

    def take(self, n: float, now: float) -> float:
        """Take ``n`` tokens; 0 when granted, else the seconds until they would be available."""
        self.tokens = min(self.burst, self.tokens + max(0.0, now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= n or (n > self.burst and self.tokens >= self.burst):
            self.tokens -= n  # may go negative for an oversized batch: debt slows what follows
            return 0.0
        return (min(n, self.burst) - self.tokens) / self.rate


def _retry_after(wait_s: float) -> float:
    return float(max(1, math.ceil(wait_s)))


class RateLimiter:
    """Request and span buckets per Project; limits follow the Project's current settings."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._requests: dict[str, TokenBucket] = {}
        self._spans: dict[str, TokenBucket] = {}

    @staticmethod
    def _bucket(
        table: dict[str, TokenBucket], project_id: str, rate: int, burst: int, now: float
    ) -> TokenBucket:
        bucket = table.get(project_id)
        if bucket is None:
            bucket = table[project_id] = TokenBucket.full(rate, burst, now)
        elif (bucket.rate, bucket.burst) != (rate, burst):  # settings changed
            bucket.rate, bucket.burst = rate, burst
            bucket.tokens = min(bucket.tokens, burst)
        return bucket

    def check_request(self, project_id: str, rate: int, burst: int) -> None:
        """REQ-007.a, checked right after authentication (before the body is read)."""
        now = self.clock()
        wait = self._bucket(self._requests, project_id, rate, burst, now).take(1, now)
        if wait:
            raise RateLimited(retry_after_s=_retry_after(wait))

    def check_spans(self, project_id: str, spans: int, rate: int, burst: int) -> None:
        """REQ-007.a, counted on the decoded spans."""
        if spans <= 0:
            return
        now = self.clock()
        wait = self._bucket(self._spans, project_id, rate, burst, now).take(spans, now)
        if wait:
            raise RateLimited(retry_after_s=_retry_after(wait))

    def forget(self, project_id: str) -> None:
        self._requests.pop(project_id, None)
        self._spans.pop(project_id, None)


# ── body caps (REQ-007.b/c) ──────────────────────────────────────────────────────────────────


def check_content_length(headers: Mapping[str, str], max_body_bytes: int) -> None:
    """Refuse before reading when the declared size is over the cap; bad values are 400."""
    for name, value in headers.items():
        if name.lower() == "content-length":
            try:
                declared = int(value)
            except ValueError:
                raise DecodeError() from None
            if declared < 0:
                raise DecodeError()
            if declared > max_body_bytes:
                raise PayloadTooLarge()
            return


async def read_limited(stream: AsyncIterable[bytes], max_body_bytes: int) -> bytes:
    """Read the body; stop as soon as it exceeds the cap (the rest is never read)."""
    parts: list[bytes] = []
    size = 0
    async for chunk in stream:
        size += len(chunk)
        if size > max_body_bytes:
            raise PayloadTooLarge()
        parts.append(chunk)
    return b"".join(parts)


def inflate_gzip(data: bytes, max_out: int) -> bytes:
    """Inflate (possibly multi-member) gzip with an output cap; corrupt input is a 400."""
    out = bytearray()
    pending = data
    try:
        while True:
            inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            while pending and not inflater.eof:
                # never ask for more than one byte past the cap: the bomb is never inflated
                out += inflater.decompress(pending, max_out - len(out) + 1)
                if len(out) > max_out:
                    raise PayloadTooLarge()
                pending = inflater.unconsumed_tail
            if not inflater.eof:
                raise DecodeError()  # truncated stream
            pending = inflater.unused_data
            if not pending:
                return bytes(out)
    except zlib.error:
        raise DecodeError() from None


def decode_content(body: bytes, content_encoding: str | None, max_decompressed: int) -> bytes:
    """Apply ``Content-Encoding`` (identity or gzip) with the decompressed cap."""
    encoding = (content_encoding or "identity").strip().lower()
    if encoding == "identity":
        if len(body) > max_decompressed:
            raise PayloadTooLarge()
        return body
    if encoding == "gzip":
        return inflate_gzip(body, max_decompressed)
    raise UnsupportedMediaType()


# ── overload (REQ-009.b) ─────────────────────────────────────────────────────────────────────


class OverloadGuard:
    def __init__(self, writer: Writer, *, commit_latency_s: float, retry_after_s: float) -> None:
        self.writer = writer
        self.commit_latency_s = commit_latency_s
        self.retry_after_s = retry_after_s

    def check(self) -> None:
        """503 before decoding: storage down, queue at its budget, or commits too slow."""
        if not self.writer.available:
            raise StorageUnavailable("storage unavailable; retry later")
        queue = self.writer.queue
        if (
            queue.pending_bytes >= queue.budget_bytes
            or self.writer.commit_latency_p50() > self.commit_latency_s
        ):
            raise Overloaded(retry_after_s=self.retry_after_s)
