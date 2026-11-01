"""The ingest engine: a dedicated thread with its own event loop (plan task P3-07.1).

Tech Design DD-001: the whole ingest pipeline (writer, gRPC server, jobs) runs on an "ingest
thread" with its own asyncio loop, so it never depends on the loop Reflex / Granian runs on.
The store is opened **on that loop** (asyncpg connections are loop-bound).

- :meth:`IngestEngine.submit` may be awaited from any loop: it hops onto the ingest loop with
  ``run_coroutine_threadsafe`` and resolves after the batch's commit (see ``ingest.writer``).
- :meth:`IngestEngine.stop` closes admission, lets the writer drain for up to ``shutdown_grace``,
  then cancels the services (gRPC stops after the drain) and closes the store (REQ-331).
- A **supervisor** task on the starting loop watches the thread. If it dies outside ``stop``
  (a bug), it logs CRITICAL, keeps the engine not ready, and restarts it with backoff; after
  ``max_restarts`` restarts within ``restart_window_s`` it stays down (Tech Design §3.3).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from reflex_agentlens.ingest.errors import ShuttingDown
from reflex_agentlens.ingest.writer import BatchOutcome, GroupStore, WriteQueue, Writer
from reflex_agentlens.store.records import NormalizedBatch

__all__ = ["EngineCrashed", "EngineRuntime", "EngineStore", "IngestEngine", "Service"]

log = logging.getLogger(__name__)


class EngineStore(GroupStore, Protocol):
    async def close(self) -> None: ...


class EngineCrashed(RuntimeError):
    """A writer or service task ended while the engine was not stopping."""


@dataclass(slots=True)
class EngineRuntime:
    """What one run of the ingest thread owns; services receive it."""

    loop: asyncio.AbstractEventLoop
    store: EngineStore
    queue: WriteQueue
    writer: Writer
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)


# a long-running coroutine on the ingest loop (gRPC server, jobs)
Service = Callable[[EngineRuntime], Coroutine[Any, Any, None]]


class IngestEngine:
    def __init__(
        self,
        open_store: Callable[[], Awaitable[EngineStore]],
        *,
        queue_memory_bytes: int,
        shutdown_grace_s: float = 20.0,
        services: Sequence[Service] = (),
        writer_options: dict[str, Any] | None = None,
        restart_backoff: tuple[float, float] = (1.0, 30.0),
        max_restarts: int = 5,
        restart_window_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        thread_name: str = "agentlens-ingest",
    ) -> None:
        self._open_store = open_store
        self.queue_memory_bytes = queue_memory_bytes
        self.shutdown_grace_s = shutdown_grace_s
        self.services = tuple(services)
        self.writer_options = dict(writer_options or {})
        self.restart_backoff = restart_backoff
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.clock = clock
        self.thread_name = thread_name

        self._runtime: EngineRuntime | None = None
        self._thread: threading.Thread | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False
        self._restarts: deque[float] = deque()
        self._pending: set[concurrent.futures.Future[BatchOutcome]] = set()
        self._pending_lock = threading.Lock()
        self.drained = True
        self.gave_up = False
        self.last_error: BaseException | None = None

    # ── state ────────────────────────────────────────────────────────────────────────────────

    @property
    def runtime(self) -> EngineRuntime | None:
        return self._runtime

    @property
    def ready(self) -> bool:
        """Readiness flag for ``/readyz`` and admission: running, not stopping, storage up."""
        rt, thread = self._runtime, self._thread
        return (
            rt is not None
            and not self._stopping
            and thread is not None
            and thread.is_alive()
            and rt.writer.available
        )

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def restarts(self) -> int:
        return len(self._restarts)

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the ingest thread and wait until its store is open; then supervise it."""
        if self._thread is not None:
            raise RuntimeError("ingest engine already started")
        self._stopping = False
        await self._launch()
        self._supervisor = asyncio.create_task(self._supervise(), name="ingest-supervisor")

    async def stop(self) -> bool:
        """REQ-331: refuse new work, drain for ``shutdown_grace``, then stop the thread.

        Returns whether everything accepted was committed before the grace period ran out.
        """
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        rt, thread = self._runtime, self._thread
        if rt is not None:
            with contextlib.suppress(RuntimeError):  # the loop already closed
                rt.loop.call_soon_threadsafe(rt.stop_requested.set)
        if thread is not None:
            await asyncio.to_thread(thread.join)
        self._thread = None
        return self.drained

    # ── submission ───────────────────────────────────────────────────────────────────────────

    async def submit(self, batch: NormalizedBatch, size: int | None = None) -> BatchOutcome:
        """Queue a batch from any loop; resolves after its commit (``ingest.writer``)."""
        rt = self._runtime
        if rt is None or self._stopping:
            raise ShuttingDown()
        with contextlib.suppress(RuntimeError):
            if asyncio.get_running_loop() is rt.loop:
                return await _put(rt, batch, size)
        try:
            cf = asyncio.run_coroutine_threadsafe(_put(rt, batch, size), rt.loop)
        except RuntimeError:  # the ingest loop is closed
            raise ShuttingDown() from None
        with self._pending_lock:
            self._pending.add(cf)
        cf.add_done_callback(self._forget)
        return await asyncio.wrap_future(cf)

    def _forget(self, cf: concurrent.futures.Future[BatchOutcome]) -> None:
        with self._pending_lock:
            self._pending.discard(cf)

    def _fail_pending(self) -> None:
        """Submissions scheduled on a loop that stopped before running them."""
        with self._pending_lock:
            pending, self._pending = self._pending, set()
        for cf in pending:
            if not cf.done():
                with contextlib.suppress(concurrent.futures.InvalidStateError):
                    cf.set_exception(ShuttingDown())

    # ── the thread ───────────────────────────────────────────────────────────────────────────

    async def _launch(self) -> None:
        started: concurrent.futures.Future[None] = concurrent.futures.Future()
        thread = threading.Thread(
            target=self._thread_main, args=(started,), name=self.thread_name, daemon=True
        )
        self._thread = thread
        thread.start()
        await asyncio.wrap_future(started)

    def _thread_main(self, started: concurrent.futures.Future[None]) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve(loop, started))
        except BaseException as exc:
            self.last_error = exc
            if not started.done():
                started.set_exception(exc)
            elif not self._stopping:
                log.critical("ingest thread crashed: %s", type(exc).__name__, exc_info=exc)
        finally:
            self._runtime = None
            self._fail_pending()
            with contextlib.suppress(Exception):
                _cancel_all(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _serve(
        self, loop: asyncio.AbstractEventLoop, started: concurrent.futures.Future[None]
    ) -> None:
        store = await self._open_store()
        queue = WriteQueue(self.queue_memory_bytes)
        writer = Writer(store, queue, **self.writer_options)
        rt = EngineRuntime(loop, store, queue, writer)
        tasks = {asyncio.create_task(writer.run(), name="ingest-writer")}
        tasks |= {asyncio.create_task(svc(rt)) for svc in self.services}
        try:
            self._runtime = rt
            self.drained = True
            started.set_result(None)
            stop = asyncio.create_task(rt.stop_requested.wait())
            done, _ = await asyncio.wait({*tasks, stop}, return_when=asyncio.FIRST_COMPLETED)
            if stop not in done:  # a task ended on its own: that is a crash
                stop.cancel()
                task = next(iter(done))
                cause = None if task.cancelled() else task.exception()
                raise EngineCrashed(f"{task.get_name()} ended unexpectedly") from cause
            self.drained = await writer.drain(self.shutdown_grace_s)
        finally:
            self._runtime = None
            queue.closed = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            queue.fail_all(ShuttingDown)
            try:
                await store.close()
            except Exception:
                log.exception("closing the ingest store failed")

    # ── supervision ──────────────────────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        delay, cap = self.restart_backoff
        while True:
            thread = self._thread
            if thread is not None:
                await asyncio.to_thread(thread.join)
            if self._stopping:
                return
            horizon = self.clock() - self.restart_window_s
            while self._restarts and self._restarts[0] < horizon:
                self._restarts.popleft()
            if len(self._restarts) >= self.max_restarts:
                self.gave_up = True
                log.critical(
                    "ingest engine crashed %d times within %.0f s; staying down",
                    len(self._restarts) + 1,
                    self.restart_window_s,
                )
                return
            log.critical("ingest engine is down; restarting in %.1f s", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, cap)
            if self.stopping:  # property: stop() may have run during the sleep
                return
            self._restarts.append(self.clock())
            try:
                await self._launch()
            except Exception as exc:
                log.critical("ingest engine restart failed: %s", type(exc).__name__)


async def _put(rt: EngineRuntime, batch: NormalizedBatch, size: int | None) -> BatchOutcome:
    # shield: a submitter that goes away must not cancel the batch's commit
    return await asyncio.shield(rt.queue.put(batch, size))


def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
    tasks = asyncio.all_tasks(loop)
    for task in tasks:
        task.cancel()
    if tasks:
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
