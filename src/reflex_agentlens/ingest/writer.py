"""The writer coroutine and its bounded queue (plan task P3-06.2; REQ-009, REQ-330, DD-003).

One writer per process drains a :class:`WriteQueue` and commits each **group** of batches
(at most ``linger_s`` after the first one arrives, or ``max_spans`` spans) with
``StorageBackend.write_group``. Every batch's future resolves only after the commit:

- ``WriteResult``: committed;
- ``BatchRejected``: that batch hit a data error and was rolled back alone (DD-003);
- exception ``StorageUnavailable``: the group failed and ``ping()`` fails too (lost connection,
  I/O, …). The writer marks storage unavailable, rejects what is queued, and retries ``ping()``
  with backoff 0.5 → 5 s until the database answers again (REQ-330.a). If the database still
  answers, the batches are retried one by one and only the one that fails again is rejected
  (``BatchRejected("invalid_data")``), so one tenant's batch cannot fail the others;
- exception ``ShuttingDown``: the process stopped before the batch could be committed.

The queue is bounded by **bytes** (REQ-009.c, ``[ingest] queue_memory_bytes``); a batch that does
not fit is refused immediately with ``Overloaded`` rather than waiting (REQ-009.b). Bytes are
released after the batch's commit, so the budget covers everything not yet durable. The writer
also keeps the commit latencies of the last 5 seconds; their median feeds admission control.

Everything here runs on the ingest loop: producers and the writer share one event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.ingest.errors import Overloaded, ShuttingDown
from reflex_agentlens.store.records import BatchRejected, NormalizedBatch, WriteResult

__all__ = ["BatchOutcome", "GroupStore", "WriteQueue", "Writer", "batch_bytes"]

log = logging.getLogger(__name__)

BatchOutcome = WriteResult | BatchRejected
_SPAN_OVERHEAD = 256  # row, keys and derived rows beyond the JSON payload


class GroupStore(Protocol):
    async def write_group(self, batches: Sequence[NormalizedBatch]) -> list[BatchOutcome]: ...

    async def ping(self) -> bool: ...


def batch_bytes(batch: NormalizedBatch) -> int:
    """Memory estimate of a queued batch (REQ-009.c)."""
    return sum(sp.size_bytes + _SPAN_OVERHEAD for sp in batch.spans)


@dataclass(slots=True)
class _Item:
    batch: NormalizedBatch
    size: int
    future: asyncio.Future[BatchOutcome]
    spans: int = field(init=False)

    def __post_init__(self) -> None:
        self.spans = len(self.batch.spans)


class _Outage(Exception):
    """The group failed and the database does not answer ``ping()``."""


class WriteQueue:
    """FIFO of batches bounded by bytes; ``put`` never waits."""

    def __init__(self, budget_bytes: int) -> None:
        self.budget_bytes = budget_bytes
        self.pending_bytes = 0  # queued + in flight, released after commit
        self._items: deque[_Item] = deque()
        self._ready = asyncio.Event()
        self.drained = asyncio.Event()  # nothing queued or in flight
        self.drained.set()
        self.closed = False
        self.available = True

    def __len__(self) -> int:
        return len(self._items)

    def put(self, batch: NormalizedBatch, size: int | None = None) -> asyncio.Future[BatchOutcome]:
        """Enqueue a batch; the future resolves after its commit (see the module docstring)."""
        if self.closed:
            raise ShuttingDown()
        if not self.available:
            raise StorageUnavailable("storage unavailable; retry later")
        need = max(1, batch_bytes(batch) if size is None else size)  # ≥ 1: drained ⇔ 0 bytes
        if self.pending_bytes + need > self.budget_bytes:
            raise Overloaded()  # fixed client message (API §3.3)
        future: asyncio.Future[BatchOutcome] = asyncio.get_running_loop().create_future()
        self._items.append(_Item(batch, need, future))
        self.pending_bytes += need
        self._ready.set()
        self.drained.clear()
        return future

    async def wait(self) -> None:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()

    def take(self, max_spans: int) -> list[_Item]:
        """The next group: at least one batch, more while the span cap allows."""
        group: list[_Item] = []
        spans = 0
        while self._items and (not group or spans + self._items[0].spans <= max_spans):
            item = self._items.popleft()
            group.append(item)
            spans += item.spans
        return group

    def queued_spans(self) -> int:
        return sum(item.spans for item in self._items)

    def release(self, items: Sequence[_Item]) -> None:
        self.pending_bytes -= sum(item.size for item in items)
        if self.pending_bytes == 0:
            self.drained.set()

    def fail_all(self, exc_factory: Callable[[], BaseException]) -> None:
        while self._items:
            item = self._items.popleft()
            self.release([item])
            if not item.future.done():
                item.future.set_exception(exc_factory())


class Writer:
    """The single writer coroutine (DD-003). Start :meth:`run` as a task on the ingest loop."""

    def __init__(
        self,
        store: GroupStore,
        queue: WriteQueue,
        *,
        linger_s: float = 0.05,
        max_spans: int = 5_000,
        latency_window_s: float = 5.0,
        backoff: tuple[float, float] = (0.5, 5.0),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.queue = queue
        self.linger_s = linger_s
        self.max_spans = max_spans
        self.latency_window_s = latency_window_s
        self.backoff = backoff
        self.clock = clock
        self._latencies: deque[tuple[float, float]] = deque()
        self._busy = False

    # ── observability for admission and readiness ────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self.queue.available

    def commit_latency_p50(self) -> float:
        """Median commit latency (seconds) over the last window; 0 without samples."""
        self._prune()
        return statistics.median(lat for _, lat in self._latencies) if self._latencies else 0.0

    def _prune(self) -> None:
        horizon = self.clock() - self.latency_window_s
        while self._latencies and self._latencies[0][0] < horizon:
            self._latencies.popleft()

    # ── the loop ─────────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        while True:
            await self.queue.wait()
            self._busy = True
            try:
                if self.queue.queued_spans() < self.max_spans:
                    await asyncio.sleep(self.linger_s)  # let the group fill (≤ 50 ms)
                group = self.queue.take(self.max_spans)
                if group:
                    await self._commit(group)
            finally:
                self._busy = False

    async def _commit(self, group: list[_Item]) -> None:
        started = self.clock()
        try:
            outcomes = await self._write_or_isolate(group)
        except asyncio.CancelledError:
            self._settle(group, lambda _: ShuttingDown())
            raise
        except _Outage:
            self._settle(group, lambda _: StorageUnavailable("commit failed; retry later"))
            await self._recover()
            return
        self._latencies.append((self.clock(), self.clock() - started))
        self._prune()
        self._settle(group, outcomes.__getitem__)

    async def _write_or_isolate(self, group: list[_Item]) -> list[BatchOutcome]:
        """Commit the group; if it fails while the database answers, commit batch by batch.

        A failing group is a storage outage only when ``ping()`` fails too. Otherwise one batch
        tripped something the savepoints did not classify, so each batch is retried alone and the
        one that fails again is rejected; the others still commit (no cross-tenant failure).
        """
        try:
            return await self.store.write_group([item.batch for item in group])
        except Exception as exc:
            log.error("write group failed (%s)", type(exc).__name__)
            if not await self._reachable():
                raise _Outage() from exc
        outcomes: list[BatchOutcome] = []
        for item in group:
            try:
                outcomes.extend(await self.store.write_group([item.batch]))
            except Exception as exc:
                if not await self._reachable():
                    raise _Outage() from exc
                log.error("batch rejected after isolation (%s)", type(exc).__name__)
                outcomes.append(BatchRejected("invalid_data"))
        return outcomes

    async def _reachable(self) -> bool:
        try:
            return await self.store.ping()
        except Exception:
            return False

    def _settle(
        self,
        group: list[_Item],
        result: Callable[[int], BatchOutcome | BaseException],
    ) -> None:
        """Release the group's bytes and resolve each still-pending future."""
        self.queue.release(group)
        for i, item in enumerate(group):
            if item.future.done():
                continue
            value = result(i)
            if isinstance(value, BaseException):
                item.future.set_exception(value)
            else:
                item.future.set_result(value)

    async def _recover(self) -> None:
        """REQ-330.a: refuse new work, then ping with backoff until the database answers."""
        self.queue.available = False
        self.queue.fail_all(lambda: StorageUnavailable("storage unavailable; retry later"))
        delay, cap = self.backoff
        while True:
            await asyncio.sleep(delay)
            with contextlib.suppress(Exception):
                if await self.store.ping():
                    break
            delay = min(delay * 2, cap)
        log.warning("storage available again")
        self.queue.available = True

    # ── shutdown (REQ-331) ───────────────────────────────────────────────────────────────────

    async def drain(self, timeout_s: float) -> bool:
        """Close the queue and wait for queued work to commit; ``False`` if time ran out."""
        self.queue.closed = True
        try:
            async with asyncio.timeout(timeout_s):
                await self.queue.drained.wait()
        except TimeoutError:
            self.queue.fail_all(ShuttingDown)
            return False
        return True
