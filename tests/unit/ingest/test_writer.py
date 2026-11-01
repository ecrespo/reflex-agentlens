"""Writer coroutine and WriteQueue (plan task P3-06.2; REQ-009.a-c, REQ-330.a, DD-003)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.ingest.errors import Overloaded, ShuttingDown
from reflex_agentlens.ingest.writer import BatchOutcome, WriteQueue, Writer, batch_bytes
from reflex_agentlens.store.records import (
    BatchRejected,
    NormalizedBatch,
    NormalizedSpan,
    ResourceIn,
    SourceKey,
    WriteResult,
)

RES = ResourceIn(b"\x00" * 16, {})


def nspan(i: int, size: int = 100) -> NormalizedSpan:
    return NormalizedSpan(
        trace_id=i.to_bytes(16, "big"), span_id=(1).to_bytes(8, "big"), parent_span_id=None,
        source=SourceKey("svc"), resource=RES, name="s", start_ns=1, end_ns=2,
        mapping_version=1, size_bytes=size, received_at_ns=2,
    )  # fmt: skip


def nbatch(project: str = "p", n: int = 1, size: int = 100) -> NormalizedBatch:
    return NormalizedBatch(project, tuple(nspan(i, size) for i in range(1, n + 1)))


class FakeStore:
    def __init__(self) -> None:
        self.groups: list[list[NormalizedBatch]] = []
        self.gate: asyncio.Event | None = None
        self.fail: BaseException | None = None
        self.reject: set[str] = set()
        self.poison: set[str] = set()  # projects whose batch raises a non-database error
        self.ping_answers: list[bool] = []
        self.pings = 0

    async def write_group(self, batches: Sequence[NormalizedBatch]) -> list[BatchOutcome]:
        self.groups.append(list(batches))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        if any(b.project_id in self.poison for b in batches):
            raise ValueError("unclassified")
        return [
            BatchRejected("project_not_found") if b.project_id in self.reject
            else WriteResult(len(b.spans), 0, [], 1)
            for b in batches
        ]  # fmt: skip

    async def ping(self) -> bool:
        self.pings += 1
        return self.ping_answers.pop(0) if self.ping_answers else True


@pytest.fixture
async def rig() -> AsyncIterator[tuple[FakeStore, WriteQueue, Writer]]:
    store = FakeStore()
    queue = WriteQueue(budget_bytes=10_000)
    writer = Writer(store, queue, linger_s=0.005, max_spans=10, backoff=(0.001, 0.004))
    task = asyncio.create_task(writer.run())
    yield store, queue, writer
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_no_future_resolves_before_its_commit(
    rig: tuple[FakeStore, WriteQueue, Writer],
) -> None:
    """REQ-009.a: the acknowledgement waits for the commit."""
    store, queue, _ = rig
    store.gate = asyncio.Event()
    fut = queue.put(nbatch())
    await asyncio.sleep(0.03)
    assert store.groups  # the commit is in progress
    assert not fut.done()
    store.gate.set()
    result = await asyncio.wait_for(fut, 1)
    assert isinstance(result, WriteResult)
    assert result.inserted == 1


async def test_batches_are_grouped_up_to_the_span_cap(
    rig: tuple[FakeStore, WriteQueue, Writer],
) -> None:
    store, queue, _ = rig
    futures = [queue.put(nbatch(n=4)) for _ in range(3)]  # 12 spans, cap 10
    await asyncio.wait_for(asyncio.gather(*futures), 1)
    assert [len(g) for g in store.groups] == [2, 1]
    oversized = queue.put(nbatch(n=25))  # one batch over the cap still goes, alone
    await asyncio.wait_for(oversized, 1)
    assert len(store.groups[-1]) == 1


async def test_rejected_batch_resolves_alone(rig: tuple[FakeStore, WriteQueue, Writer]) -> None:
    store, queue, _ = rig
    store.reject = {"gone"}
    ok, bad = queue.put(nbatch("p")), queue.put(nbatch("gone"))
    assert isinstance(await asyncio.wait_for(ok, 1), WriteResult)
    assert await asyncio.wait_for(bad, 1) == BatchRejected("project_not_found")


async def test_unclassified_failure_isolates_the_batch_not_the_group(
    rig: tuple[FakeStore, WriteQueue, Writer],
) -> None:
    """A batch that raises while the database answers fails alone; storage stays available."""
    store, queue, writer = rig
    store.poison = {"bad"}
    ok, bad, ok2 = queue.put(nbatch("a")), queue.put(nbatch("bad")), queue.put(nbatch("b"))
    assert isinstance(await asyncio.wait_for(ok, 1), WriteResult)
    assert await asyncio.wait_for(bad, 1) == BatchRejected("invalid_data")
    assert isinstance(await asyncio.wait_for(ok2, 1), WriteResult)
    assert writer.available
    assert [len(g) for g in store.groups] == [3, 1, 1, 1]
    assert queue.pending_bytes == 0


async def test_full_queue_rejects_immediately_and_frees_after_commit(
    rig: tuple[FakeStore, WriteQueue, Writer],
) -> None:
    """REQ-009.b/c: no waiting for room; bytes are held until the commit."""
    store, queue, _ = rig
    store.gate = asyncio.Event()
    first = queue.put(nbatch(), size=8_000)
    with pytest.raises(Overloaded):
        queue.put(nbatch(), size=3_000)
    assert queue.pending_bytes == 8_000
    store.gate.set()
    await asyncio.wait_for(first, 1)
    assert queue.pending_bytes == 0
    await asyncio.wait_for(queue.put(nbatch(), size=3_000), 1)


async def test_outage_then_automatic_recovery(rig: tuple[FakeStore, WriteQueue, Writer]) -> None:
    """REQ-330.a: StorageUnavailable while down, ping backoff, then back without restart."""
    store, queue, writer = rig
    store.fail = OSError("connection lost")
    store.gate = asyncio.Event()
    in_flight = queue.put(nbatch())
    await asyncio.sleep(0.02)
    queued = queue.put(nbatch())  # waits behind the failing group
    store.ping_answers = [False, False, True]
    store.gate.set()
    with pytest.raises(StorageUnavailable):
        await asyncio.wait_for(in_flight, 1)
    with pytest.raises(StorageUnavailable):
        await asyncio.wait_for(queued, 1)
    assert not writer.available
    with pytest.raises(StorageUnavailable):
        queue.put(nbatch())
    store.fail, store.gate = None, None
    async with asyncio.timeout(1):
        while not bool(getattr(writer, "available")):  # noqa: ASYNC110, B009 - mypy narrowing
            await asyncio.sleep(0.001)
    assert store.pings == 3
    assert queue.pending_bytes == 0
    assert isinstance(await asyncio.wait_for(queue.put(nbatch()), 1), WriteResult)


async def test_commit_latency_median_over_the_window() -> None:
    now = [100.0]
    store = FakeStore()
    queue = WriteQueue(10_000)
    writer = Writer(store, queue, linger_s=0, clock=lambda: now[0])
    assert writer.commit_latency_p50() == 0.0
    writer._latencies.extend([(96.0, 9.0), (97.0, 1.0), (98.0, 2.0), (99.0, 3.0)])
    assert writer.commit_latency_p50() == 2.5
    now[0] = 103.5  # the samples older than 5 s drop out
    assert writer.commit_latency_p50() == 3.0


async def test_drain_commits_queued_work_then_refuses(
    rig: tuple[FakeStore, WriteQueue, Writer],
) -> None:
    """REQ-331.a: stop accepting, finish what was accepted."""
    _, queue, writer = rig
    futures = [queue.put(nbatch()) for _ in range(3)]
    assert await writer.drain(1.0)
    assert all(f.done() and isinstance(f.result(), WriteResult) for f in futures)
    with pytest.raises(ShuttingDown):
        queue.put(nbatch())


async def test_drain_timeout_fails_what_is_left(rig: tuple[FakeStore, WriteQueue, Writer]) -> None:
    store, queue, writer = rig
    store.gate = asyncio.Event()  # the first commit never finishes
    first = queue.put(nbatch(n=10))
    await asyncio.sleep(0.02)
    second = queue.put(nbatch())
    assert not await writer.drain(0.05)
    with pytest.raises(ShuttingDown):
        await second
    assert not first.done()  # still in flight: the engine cancels it on stop
    store.gate.set()
    assert isinstance(await asyncio.wait_for(first, 1), WriteResult)


async def test_cancel_during_commit_fails_in_flight_futures() -> None:
    store = FakeStore()
    store.gate = asyncio.Event()
    queue = WriteQueue(10_000)
    task = asyncio.create_task(Writer(store, queue, linger_s=0).run())
    fut = queue.put(nbatch())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ShuttingDown):
        await fut
    assert queue.pending_bytes == 0


def test_batch_bytes_estimate() -> None:
    assert batch_bytes(nbatch(n=2, size=1000)) == 2 * (1000 + 256)
