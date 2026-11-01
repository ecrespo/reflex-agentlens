"""Ingest engine: thread, loop, supervisor (plan task P3-07.1; DD-001, REQ-331)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Sequence

import pytest

from reflex_agentlens.ingest.engine import EngineRuntime, IngestEngine
from reflex_agentlens.ingest.errors import ShuttingDown
from reflex_agentlens.ingest.writer import BatchOutcome
from reflex_agentlens.store.records import NormalizedBatch, WriteResult

from .test_writer import nbatch


class SlowStore:
    """Commits after ``delay`` seconds on whatever loop it runs on; records that thread."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.threads: set[int] = set()
        self.committed: list[str] = []
        self.closed = 0

    async def write_group(self, batches: Sequence[NormalizedBatch]) -> list[BatchOutcome]:
        self.threads.add(threading.get_ident())
        await asyncio.sleep(self.delay)
        self.committed.extend(b.project_id for b in batches)
        return [WriteResult(len(b.spans), 0, [], 1) for b in batches]

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed += 1


def engine_for(store: SlowStore, **kwargs: object) -> IngestEngine:
    async def open_store() -> SlowStore:
        return store

    options: dict[str, object] = {"queue_memory_bytes": 1 << 20, "shutdown_grace_s": 1.0}
    options.update(kwargs)
    return IngestEngine(
        open_store,
        writer_options={"linger_s": 0.001, "backoff": (0.001, 0.002)},
        **options,  # type: ignore[arg-type]
    )


@pytest.fixture
async def running() -> AsyncIterator[tuple[SlowStore, IngestEngine]]:
    store = SlowStore()
    engine = engine_for(store)
    await engine.start()
    yield store, engine
    await engine.stop()


async def test_submit_from_the_main_loop_commits_on_the_ingest_thread(
    running: tuple[SlowStore, IngestEngine],
) -> None:
    store, engine = running
    assert engine.ready
    result = await asyncio.wait_for(engine.submit(nbatch("p")), 2)
    assert isinstance(result, WriteResult)
    assert store.committed == ["p"]
    assert store.threads
    assert threading.get_ident() not in store.threads


async def test_stop_drains_accepted_work_then_refuses() -> None:
    """REQ-331.a: everything accepted before stop is committed; later submits fail fast."""
    store = SlowStore(delay=0.02)
    engine = engine_for(store)
    await engine.start()
    pending = [asyncio.create_task(engine.submit(nbatch(f"p{i}"))) for i in range(5)]
    await asyncio.sleep(0.01)  # let them reach the ingest queue
    assert await engine.stop()
    results = await asyncio.gather(*pending)
    assert all(isinstance(r, WriteResult) for r in results)
    assert sorted(store.committed) == [f"p{i}" for i in range(5)]
    assert store.closed == 1
    assert not engine.ready
    with pytest.raises(ShuttingDown):
        await engine.submit(nbatch())


async def test_stop_after_grace_fails_what_did_not_commit() -> None:
    store = SlowStore(delay=5.0)
    engine = engine_for(store, shutdown_grace_s=0.05)
    await engine.start()
    task = asyncio.create_task(engine.submit(nbatch()))
    await asyncio.sleep(0.01)
    assert not await asyncio.wait_for(engine.stop(), 2)
    with pytest.raises(ShuttingDown):
        await asyncio.wait_for(task, 2)
    assert store.closed == 1


async def test_submit_before_start_is_refused() -> None:
    with pytest.raises(ShuttingDown):
        await engine_for(SlowStore()).submit(nbatch())


async def test_store_open_failure_fails_start() -> None:
    async def broken() -> SlowStore:
        raise OSError("no database")

    engine = IngestEngine(broken, queue_memory_bytes=1 << 20)
    with pytest.raises(OSError, match="no database"):
        await engine.start()
    assert not engine.ready


def crashing_service(crashes: list[int]) -> object:
    """A service that crashes the engine while ``crashes[0] > 0``, else idles."""

    async def service(rt: EngineRuntime) -> None:
        if crashes[0] > 0:
            crashes[0] -= 1
            raise RuntimeError("bug")
        await asyncio.Event().wait()

    return service


async def wait_until(cond: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not cond():  # noqa: ASYNC110 - state set by the ingest thread
            await asyncio.sleep(0.005)


async def test_crash_restarts_with_backoff_and_recovers() -> None:
    crashes = [3]
    store = SlowStore()
    engine = engine_for(store, services=[crashing_service(crashes)], restart_backoff=(0.01, 0.02))
    await engine.start()
    await wait_until(lambda: crashes[0] == 0 and engine.ready)
    assert engine.restarts == 3
    assert not engine.gave_up
    assert isinstance(await asyncio.wait_for(engine.submit(nbatch()), 2), WriteResult)
    await engine.stop()
    assert store.closed == 4  # one store per run, each closed


async def test_five_crashes_in_the_window_then_stays_down() -> None:
    crashes = [100]
    engine = engine_for(
        SlowStore(), services=[crashing_service(crashes)], restart_backoff=(0.001, 0.002)
    )
    await engine.start()
    await wait_until(lambda: engine.gave_up)
    assert engine.restarts == 5
    assert crashes[0] == 100 - 6  # the first run plus five restarts
    assert not engine.ready
    with pytest.raises(ShuttingDown):
        await engine.submit(nbatch())
    await engine.stop()


async def test_restarts_older_than_the_window_do_not_count() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 400.0  # every observation is 400 s later: never 5 within 600 s
        return now[0]

    crashes = [8]
    engine = engine_for(
        SlowStore(),
        services=[crashing_service(crashes)],
        restart_backoff=(0.001, 0.002),
        clock=clock,
    )
    await engine.start()
    await wait_until(lambda: crashes[0] == 0 and engine.ready)
    assert not engine.gave_up
    await engine.stop()


async def test_readiness_follows_storage_availability(
    running: tuple[SlowStore, IngestEngine],
) -> None:
    _, engine = running
    rt = engine.runtime
    assert rt is not None
    rt.queue.available = False
    assert not engine.ready
    rt.queue.available = True
    assert engine.ready


async def test_double_start_is_an_error(running: tuple[SlowStore, IngestEngine]) -> None:
    _, engine = running
    with pytest.raises(RuntimeError, match="already started"):
        await engine.start()
