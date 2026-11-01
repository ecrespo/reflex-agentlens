"""Ingest limits: token buckets, body caps, zip bombs, overload (plan task P3-08.2)."""

from __future__ import annotations

import gzip
import tracemalloc
import zlib
from collections.abc import AsyncIterator

import pytest

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.ingest.errors import (
    DecodeError,
    Overloaded,
    PayloadTooLarge,
    RateLimited,
    UnsupportedMediaType,
)
from reflex_agentlens.ingest.limits import (
    OverloadGuard,
    RateLimiter,
    TokenBucket,
    check_content_length,
    decode_content,
    inflate_gzip,
    read_limited,
)
from reflex_agentlens.ingest.writer import WriteQueue, Writer

from .test_writer import FakeStore, nbatch

MiB = 1 << 20


# ── token buckets ────────────────────────────────────────────────────────────────────────────


def test_bucket_math_with_a_fake_clock() -> None:
    bucket = TokenBucket.full(rate=10, burst=20, now=0)
    assert bucket.take(20, 0) == 0
    assert bucket.take(1, 0) == pytest.approx(0.1)  # one token every 100 ms
    assert bucket.take(5, 0.5) == 0  # refilled 5
    assert bucket.take(30, 100) == 0  # oversized: granted from a full bucket, into debt
    assert bucket.tokens == -10
    assert bucket.take(1, 100) == pytest.approx(1.1)


def test_request_limit_retry_after_is_at_least_one_second() -> None:
    """REQ-007.a."""
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    for _ in range(3):
        limiter.check_request("p", rate=50, burst=3)
    with pytest.raises(RateLimited) as err:
        limiter.check_request("p", rate=50, burst=3)
    assert err.value.retry_after_s == 1.0  # 20 ms rounds up to the 1 s floor
    assert err.value.http_status == 429
    limiter.check_request("other", rate=50, burst=3)  # Projects are independent
    now[0] = 0.02
    limiter.check_request("p", rate=50, burst=3)


def test_span_limit_reports_the_deficit_in_whole_seconds() -> None:
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    limiter.check_spans("p", 1_000, rate=100, burst=1_000)
    with pytest.raises(RateLimited) as err:
        limiter.check_spans("p", 250, rate=100, burst=1_000)
    assert err.value.retry_after_s == 3.0  # 2.5 s rounded up
    limiter.check_spans("p", 0, rate=100, burst=1_000)  # nothing to count


def test_settings_change_resizes_the_bucket() -> None:
    limiter = RateLimiter(clock=lambda: 0.0)
    limiter.check_request("p", rate=1, burst=10)
    limiter.check_request("p", rate=1, burst=1)  # burst lowered: 9 left → capped at 1
    with pytest.raises(RateLimited):
        limiter.check_request("p", rate=1, burst=1)
    limiter.forget("p")
    limiter.check_request("p", rate=1, burst=1)


# ── body caps ────────────────────────────────────────────────────────────────────────────────


def test_content_length_precheck() -> None:
    """REQ-007.b: refused before reading."""
    check_content_length({"Content-Length": "8"}, 8)
    check_content_length({}, 8)
    with pytest.raises(PayloadTooLarge):
        check_content_length({"content-length": "9"}, 8)
    for bad in ("x", "-1"):
        with pytest.raises(DecodeError):
            check_content_length({"content-length": bad}, 8)


async def test_read_stops_at_the_cap_without_draining_the_stream() -> None:
    served: list[int] = []

    async def stream() -> AsyncIterator[bytes]:
        for i in range(100):
            served.append(i)
            yield b"x" * 1_000

    assert len(await read_limited(stream(), 100_000)) == 100_000
    served.clear()
    with pytest.raises(PayloadTooLarge):
        await read_limited(stream(), 2_500)
    assert served == [0, 1, 2]


def test_gzip_round_trip_and_multi_member() -> None:
    body = b"hello " * 1_000
    assert decode_content(gzip.compress(body), "gzip", MiB) == body
    assert decode_content(gzip.compress(b"a") + gzip.compress(b"b"), " GZIP ", MiB) == b"ab"
    assert decode_content(body, None, MiB) == body
    assert decode_content(body, "identity", MiB) == body


def test_identity_body_over_the_decompressed_cap() -> None:
    with pytest.raises(PayloadTooLarge):
        decode_content(b"x" * 11, "identity", 10)


@pytest.mark.parametrize("data", [b"not gzip", gzip.compress(b"x" * 100)[:-6], b""])
def test_corrupt_or_truncated_gzip_is_a_decode_error(data: bytes) -> None:
    with pytest.raises(DecodeError):
        inflate_gzip(data, MiB)


def test_unsupported_encoding_is_415() -> None:
    with pytest.raises(UnsupportedMediaType):
        decode_content(b"", "br", MiB)


def test_gzip_bomb_stops_at_the_cap_without_allocating_it() -> None:
    """REQ-007.c: 1 GiB of zeros (~1 MiB compressed) against a 32 MiB cap."""
    deflater = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    zeros = bytes(MiB)
    bomb = b"".join(deflater.compress(zeros) for _ in range(1024)) + deflater.flush()
    assert len(bomb) < 2 * MiB
    tracemalloc.start()
    try:
        with pytest.raises(PayloadTooLarge):
            decode_content(bomb, "gzip", 32 * MiB)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 80 * MiB  # bounded by the cap, nowhere near 1 GiB


def test_exactly_at_the_cap_is_allowed() -> None:
    assert len(inflate_gzip(gzip.compress(b"z" * 1_000), 1_000)) == 1_000
    with pytest.raises(PayloadTooLarge):
        inflate_gzip(gzip.compress(b"z" * 1_001), 1_000)


# ── overload ─────────────────────────────────────────────────────────────────────────────────


async def test_overload_threshold_flips_503_on_and_off() -> None:
    """REQ-009.b: queue at budget or slow commits → 503 with the configured Retry-After."""
    now = [100.0]
    queue = WriteQueue(budget_bytes=1_000)
    writer = Writer(FakeStore(), queue, clock=lambda: now[0])
    guard = OverloadGuard(writer, commit_latency_s=2.0, retry_after_s=2.0)
    guard.check()

    writer._latencies.extend([(99.0, 3.0), (99.5, 2.5)])
    with pytest.raises(Overloaded) as err:
        guard.check()
    assert err.value.retry_after_s == 2.0
    assert err.value.http_status == 503
    now[0] = 105.0  # the slow commits leave the 5 s window
    guard.check()

    queued = queue.put(nbatch(), size=1_000)
    with pytest.raises(Overloaded):
        guard.check()
    queue.fail_all(RuntimeError)
    assert isinstance(queued.exception(), RuntimeError)
    guard.check()

    queue.available = False
    with pytest.raises(StorageUnavailable):
        guard.check()
