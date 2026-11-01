"""Engine jobs: cadence, job locks, finalizer and purge rounds (plan task P3-07.2)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from reflex_agentlens.core.config import load_config
from reflex_agentlens.ingest.engine import EngineRuntime
from reflex_agentlens.ingest.jobs import (
    FINALIZE_INTERVAL_S,
    Jobs,
    JobStore,
    SettingsCache,
    every,
    job_services,
)
from reflex_agentlens.store.changefeed import ChangeFeed
from reflex_agentlens.store.records import PurgeReport, RunKey

NOW = 10_000 * 1_000_000_000


class FakeJobStore:
    def __init__(self) -> None:
        self.changes = ChangeFeed()
        self.held: set[str] = set()
        self.stale = 0
        self.finalize_calls: list[tuple[int, int]] = []
        self.purges: list[str] = []

    @contextlib.asynccontextmanager
    async def job_lock(self, name: str) -> AsyncIterator[bool]:
        if name in self.held:
            yield False
            return
        self.held.add(name)
        try:
            yield True
        finally:
            self.held.discard(name)

    async def finalize_stale_runs(self, older_than_ns: int, limit: int) -> list[RunKey]:
        self.finalize_calls.append((older_than_ns, limit))
        n = min(limit, self.stale)
        self.stale -= n
        return [RunKey("p", i.to_bytes(16, "big")) for i in range(n)]

    async def purge_by_age(self, now_ns: int, batch_traces: int, **_: Any) -> PurgeReport:
        self.purges.append(f"age@{now_ns}/{batch_traces}")
        return PurgeReport("scheduled", now_ns, now_ns, 1, 0, 10)

    async def purge_by_size(self, limit_bytes: int, batch_traces: int, **_: Any) -> PurgeReport:
        self.purges.append(f"size<={limit_bytes}/{batch_traces}")
        return PurgeReport("scheduled", NOW, NOW, 0, 2, 20)


def jobs_for(store: FakeJobStore, **kw: Any) -> Jobs:
    return Jobs(cast(JobStore, store), finalization_window_s=300, clock_ns=lambda: NOW, **kw)


async def test_finalizer_drains_in_bounded_rounds() -> None:
    """REQ-032.b: Runs silent past the window are finalized, ``limit`` at a time."""
    store = FakeJobStore()
    store.stale = 1_200
    assert await jobs_for(store, finalize_limit=500).finalize() == 1_200
    cutoff = NOW - 300 * 1_000_000_000
    assert store.finalize_calls == [(cutoff, 500)] * 3


async def test_purge_by_age_then_size_when_limited() -> None:
    store = FakeJobStore()
    assert len(await jobs_for(store).purge() or []) == 1
    assert len(await jobs_for(store, storage_size_limit=1_000).purge() or []) == 2
    assert store.purges == [f"age@{NOW}/500", f"age@{NOW}/500", "size<=1000/500"]


async def test_a_second_job_holder_skips() -> None:
    """REQ-092.b: while another holder has the lock, the round is skipped."""
    store = FakeJobStore()
    store.stale = 3
    store.held = {"finalizer", "retention"}
    jobs = jobs_for(store)
    assert await jobs.finalize() is None
    assert await jobs.purge() is None
    assert store.finalize_calls == []
    assert store.purges == []


class FakeSleep:
    """Records requested delays; cancels the job after ``rounds`` sleeps."""

    def __init__(self, rounds: int) -> None:
        self.rounds = rounds
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) > self.rounds:
            raise asyncio.CancelledError


async def test_every_keeps_cadence_and_survives_failures() -> None:
    calls: list[int] = []

    async def job() -> None:
        calls.append(len(calls))
        if len(calls) == 2:
            raise RuntimeError("one bad round")

    sleep = FakeSleep(rounds=4)
    with pytest.raises(asyncio.CancelledError):
        await every(60, "job", job, sleep)
    assert sleep.delays == [60] * 5
    assert calls == [0, 1, 2, 3]


async def test_job_services_cadence(tmp_path: Path) -> None:
    """Finalizer every minute, purge every ``purge_interval``, settings refresh periodically."""
    toml = tmp_path / "agentlens.toml"
    toml.write_text('[retention]\npurge_interval = "15m"\n')
    cfg = load_config(config_path=toml, env={})
    store = FakeJobStore()
    rt = cast(EngineRuntime, type("RT", (), {"store": store})())
    cache = SettingsCache(cast(JobStore, store), cfg)
    delays = {}
    for index, name in [(0, "finalizer"), (1, "purge"), (3, "settings_refresh")]:
        sleep = FakeSleep(rounds=0)
        service = job_services(cfg, lambda _: cache, sleep=sleep)[index]
        with pytest.raises(asyncio.CancelledError):
            await service(rt)
        delays[name] = sleep.delays
    assert delays == {
        "finalizer": [FINALIZE_INTERVAL_S],
        "purge": [900.0],
        "settings_refresh": [30.0],
    }
