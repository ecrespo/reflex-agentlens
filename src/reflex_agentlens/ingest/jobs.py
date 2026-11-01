"""Engine jobs and the ingest settings cache (plan task P3-07.2).

Background services of the ingest engine (``ingest.engine.Service``):

- **finalizer** every minute: Q12, ``in_progress`` Runs silent for longer than the finalization
  window become ``incomplete`` (REQ-032.b/c). Late spans for such Runs are recomputed by the
  write path itself (REQ-037.c, DD-004).
- **purge** every ``[retention] purge_interval``: by age, then by size when a limit is set
  (REQ-090, REQ-091, REQ-092.a).
- **settings refresh**: the :class:`SettingsCache` drops a Project's entry on its
  ``settings_changed`` event (REQ-006.c, DD-007) and reloads everything periodically, which also
  picks up new pricing versions.

Each job runs under ``job_lock`` (REQ-092.b), so with several processes on one database only one
holder works; the others skip that round. A failing round is logged and retried next interval.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Set
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, cast

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import NotFound
from reflex_agentlens.ingest.cost import PricingSnapshot
from reflex_agentlens.ingest.engine import EngineRuntime, Service
from reflex_agentlens.ingest.normalize.pipeline import BatchContext
from reflex_agentlens.ingest.redaction.hook import RedactionHook
from reflex_agentlens.ingest.redaction.rules import CompiledRules, compile_rules
from reflex_agentlens.store.changefeed import ChangeFeed
from reflex_agentlens.store.records import (
    IngestSettings,
    JobName,
    PricingVersion,
    PricingVersionMeta,
    Project,
    PurgeReport,
    PurgeTrigger,
    RunKey,
    SecretName,
)

__all__ = [
    "FINALIZE_INTERVAL_S",
    "JobStore",
    "Jobs",
    "ProjectSnapshot",
    "SettingsCache",
    "every",
    "job_services",
]

log = logging.getLogger(__name__)

FINALIZE_INTERVAL_S = 60.0
SETTINGS_REFRESH_S = 30.0
_NS = 1_000_000_000

Sleep = Callable[[float], Awaitable[None]]


class JobStore(Protocol):
    """The store methods the jobs and the settings cache use."""

    changes: ChangeFeed

    async def list_projects(self) -> list[Project]: ...
    async def get_ingest_settings(self, project_id: str) -> IngestSettings: ...
    async def list_pricing_versions(self) -> list[PricingVersionMeta]: ...
    async def get_pricing(self, at_ns: int | None = None) -> PricingVersion: ...
    async def get_or_create_secret(self, name: SecretName) -> bytes: ...
    async def finalize_stale_runs(self, older_than_ns: int, limit: int) -> list[RunKey]: ...
    async def purge_by_age(
        self, now_ns: int, batch_traces: int, *, trigger: PurgeTrigger = "scheduled"
    ) -> PurgeReport: ...
    async def purge_by_size(
        self, limit_bytes: int, batch_traces: int, *, trigger: PurgeTrigger = "scheduled"
    ) -> PurgeReport: ...
    def job_lock(self, name: JobName) -> AbstractAsyncContextManager[bool]: ...


async def every(
    interval_s: float, name: str, fn: Callable[[], Awaitable[object]], sleep: Sleep
) -> None:
    """Run ``fn`` every ``interval_s`` (after the first interval) until cancelled."""
    while True:
        await sleep(interval_s)
        try:
            await fn()
        except Exception as exc:
            log.error("job %s failed: %s", name, type(exc).__name__)


# ── jobs ─────────────────────────────────────────────────────────────────────────────────────


class Jobs:
    def __init__(
        self,
        store: JobStore,
        *,
        finalization_window_s: float,
        purge_batch_traces: int = 500,
        storage_size_limit: int | None = None,
        finalize_limit: int = 500,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.store = store
        self.finalization_window_ns = int(finalization_window_s * _NS)
        self.purge_batch_traces = purge_batch_traces
        self.storage_size_limit = storage_size_limit
        self.finalize_limit = finalize_limit
        self.clock_ns = clock_ns

    async def finalize(self) -> int | None:
        """Q12 until no stale Run is left; the number finalized, or ``None`` if skipped."""
        async with self.store.job_lock("finalizer") as acquired:
            if not acquired:
                return None
            cutoff = self.clock_ns() - self.finalization_window_ns
            total = 0
            while True:
                keys = await self.store.finalize_stale_runs(cutoff, self.finalize_limit)
                total += len(keys)
                if len(keys) < self.finalize_limit:
                    return total

    async def purge(self) -> list[PurgeReport] | None:
        """REQ-092.a: purge by age, then by size; ``None`` if another holder runs it."""
        async with self.store.job_lock("retention") as acquired:
            if not acquired:
                return None
            reports = [await self.store.purge_by_age(self.clock_ns(), self.purge_batch_traces)]
            if self.storage_size_limit is not None:
                reports.append(
                    await self.store.purge_by_size(self.storage_size_limit, self.purge_batch_traces)
                )
            return reports


# ── settings cache ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    settings: IngestSettings
    rules: CompiledRules
    retention_max_age_s: int


class SettingsCache:
    """Per-Project ingest settings, all pricing versions and the session key, held in memory.

    Reads are plain dictionary lookups. A Project missing from the cache is loaded on first use;
    ``settings_changed`` drops it, and :meth:`refresh` reloads everything.
    """

    def __init__(
        self,
        store: JobStore,
        cfg: AgentlensConfig,
        *,
        hook: RedactionHook | None = None,
    ) -> None:
        self.store = store
        self.max_attribute_value_bytes = cfg.ingest.max_attribute_value_bytes
        self.max_clock_skew_ns = int(cfg.ingest.max_clock_skew.total_seconds() * _NS)
        self.hook = hook
        self._projects: dict[str, ProjectSnapshot] = {}
        self._retention: dict[str, int] = {}
        self.pricing = PricingSnapshot(())
        self.session_secret = b""
        self.loaded = False

    async def refresh(self) -> None:
        """Reload pricing, the Project list and the session key; drop per-Project entries."""
        self.session_secret = await self.store.get_or_create_secret("session_key")
        self.pricing = PricingSnapshot(await self._pricing_versions())
        projects = await self.store.list_projects()
        self._retention = {p.id: p.retention_max_age_s for p in projects}
        self._projects = {}
        self.loaded = True

    async def _pricing_versions(self) -> list[PricingVersion]:
        # get_pricing(effective_from) of each version yields every version PricingSnapshot.at
        # can pick (the newest one in effect at any instant has its own effective_from).
        found: dict[int, PricingVersion] = {}
        for meta in await self.store.list_pricing_versions():
            if meta.version in found:
                continue
            try:
                version = await self.store.get_pricing(meta.effective_from_ns)
            except NotFound:
                continue
            found[version.meta.version] = version
        return list(found.values())

    def invalidate(self, project_id: str) -> None:
        self._projects.pop(project_id, None)
        self._retention.pop(project_id, None)

    async def project(self, project_id: str) -> ProjectSnapshot:
        """``NotFound`` when the Project does not exist (deleted while a key was cached)."""
        if not self.loaded:
            await self.refresh()
        snap = self._projects.get(project_id)
        if snap is not None:
            return snap
        settings = await self.store.get_ingest_settings(project_id)
        if project_id not in self._retention:
            for p in await self.store.list_projects():
                self._retention[p.id] = p.retention_max_age_s
        snap = ProjectSnapshot(
            settings, compile_rules(settings.redaction), self._retention.get(project_id, 0)
        )
        self._projects[project_id] = snap
        return snap

    async def context(self, project_id: str, received_at_ns: int) -> BatchContext:
        """The normalizer's view of a Project at receive time."""
        snap = await self.project(project_id)
        return BatchContext(
            project_id=project_id,
            received_at_ns=received_at_ns,
            rules=snap.rules,
            session_secret=self.session_secret,
            pricing=self.pricing,
            content_storage=snap.settings.content_storage,
            max_attribute_value_bytes=self.max_attribute_value_bytes,
            max_clock_skew_ns=self.max_clock_skew_ns,
            retention_max_age_s=snap.retention_max_age_s,
            hook=self.hook,
        )

    async def watch(self, kinds: Set[str] = frozenset({"settings_changed"})) -> None:
        """Drop a Project's entry as soon as its ``settings_changed`` event arrives."""
        async with self.store.changes.subscribe() as events:
            async for event in events:
                if event.kind in kinds:
                    self.invalidate(event.project_id)


# ── wiring ───────────────────────────────────────────────────────────────────────────────────


def job_services(
    cfg: AgentlensConfig,
    cache_for: Callable[[JobStore], SettingsCache],
    *,
    sleep: Sleep = asyncio.sleep,
    clock_ns: Callable[[], int] = time.time_ns,
) -> list[Service]:
    """The engine services: finalizer, purge, settings watcher and periodic refresh.

    ``cache_for`` builds (or returns) the cache bound to the run's store, so the ingest routes
    and these services share one cache per engine run.
    """
    retention = cfg.retention

    def jobs(rt: EngineRuntime) -> Jobs:
        return Jobs(
            cast(JobStore, rt.store),
            finalization_window_s=cfg.ingest.finalization_window.total_seconds(),
            purge_batch_traces=retention.purge_batch_traces,
            storage_size_limit=retention.storage_size_limit,
            clock_ns=clock_ns,
        )

    async def finalizer(rt: EngineRuntime) -> None:
        await every(FINALIZE_INTERVAL_S, "finalizer", jobs(rt).finalize, sleep)

    async def purge(rt: EngineRuntime) -> None:
        await every(retention.purge_interval.total_seconds(), "purge", jobs(rt).purge, sleep)

    async def settings_watch(rt: EngineRuntime) -> None:
        await cache_for(cast(JobStore, rt.store)).watch()

    async def settings_refresh(rt: EngineRuntime) -> None:
        cache = cache_for(cast(JobStore, rt.store))
        await every(SETTINGS_REFRESH_S, "settings_refresh", cache.refresh, sleep)

    return [finalizer, purge, settings_watch, settings_refresh]
