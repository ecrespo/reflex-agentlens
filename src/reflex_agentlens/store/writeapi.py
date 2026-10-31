"""Settings and metric writes shared by both SQL backends (plan tasks P2-07.4, P2-08.1).

Backends provide ``_on_writer`` (one transaction on the write path: SQLite's single
``BEGIN IMMEDIATE`` connection under its lock, a pooled transaction on PostgreSQL) and ``_clock``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy.exc import IntegrityError

from reflex_agentlens.store import admin, maintenance, metrics, retention, settings
from reflex_agentlens.store.readapi import ReadMethods
from reflex_agentlens.store.records import (
    ApiKeyRecord,
    IngestSettings,
    JobName,
    MetricPoint,
    NewApiKeyRecord,
    NewProject,
    NewSavedFilter,
    PricingEntry,
    PricingSource,
    PricingVersion,
    PricingVersionMeta,
    Project,
    ProjectPatch,
    ProjectScope,
    PurgeReport,
    PurgeTrigger,
    RebuildReport,
    RebuildScope,
    RedactionRuleSet,
    RunKey,
    SavedFilter,
    SpanDeriver,
    StorageUsage,
    TimeRange,
    UnpricedModel,
)

__all__ = ["BackendMethods"]

_R = TypeVar("_R")


class BackendMethods(ReadMethods):
    """Mixin: reads, aggregates, metric points and settings storage."""

    _clock: Callable[[], int]
    finalization_window_ns: int

    async def _on_writer(self, fn: Callable[..., _R], *args: Any) -> _R:  # pragma: no cover
        raise NotImplementedError

    # ── metric points (P2-07.4) ──────────────────────────────────────────────────────────────

    async def write_metric_points(self, points: Sequence[MetricPoint]) -> int:
        if not points:
            return 0
        return await self._on_writer(metrics.write_metric_points, points)

    # ── Projects and API keys (P2-08.1) ──────────────────────────────────────────────────────

    async def list_projects(self) -> list[Project]:
        return await self._on_read_pool(admin.list_projects)

    async def create_project(self, new: NewProject) -> Project:
        return await self._on_writer(admin.create_project, new, self._clock())

    async def update_project(self, project_id: str, patch: ProjectPatch) -> Project:
        return await self._on_writer(admin.update_project, project_id, patch, self._clock())

    async def delete_project(self, project_id: str) -> None:
        await self._on_writer(admin.delete_project, project_id)

    async def create_api_key(self, rec: NewApiKeyRecord) -> ApiKeyRecord:
        return await self._on_writer(admin.create_api_key, rec)

    async def list_api_keys(self, project_id: str) -> list[ApiKeyRecord]:
        return await self._on_read_pool(admin.list_api_keys, project_id)

    async def resolve_api_key(self, key_id: str) -> ApiKeyRecord | None:
        return await self._on_read_pool(admin.resolve_api_key, key_id)

    async def touch_api_key(self, key_id: str, at_ns: int) -> None:
        await self._on_writer(admin.touch_api_key, key_id, at_ns)

    async def revoke_api_key(self, key_id: str, at_ns: int) -> None:
        await self._on_writer(admin.revoke_api_key, key_id, at_ns)

    # ── pricing, redaction, ingest settings, saved filters (P2-08.2) ─────────────────────────

    async def save_pricing(
        self,
        entries: Sequence[PricingEntry],
        author: str | None,
        *,
        effective_from_ns: int | None = None,
        source: PricingSource = "editor",
        note: str | None = None,
    ) -> PricingVersion:
        return await self._on_writer(
            settings.save_pricing, entries, author, self._clock(), effective_from_ns, source, note
        )

    async def list_pricing_versions(self) -> list[PricingVersionMeta]:
        return await self._on_read_pool(settings.list_pricing_versions)

    async def get_pricing(self, at_ns: int | None = None) -> PricingVersion:
        at = self._clock() if at_ns is None else at_ns
        return await self._on_read_pool(settings.get_pricing, at)

    async def list_unpriced_models(self, scope: ProjectScope) -> list[UnpricedModel]:
        return await self._on_read_pool(settings.list_unpriced_models, scope)

    async def save_redaction_rules(self, project_id: str, rules: RedactionRuleSet) -> None:
        await self._on_writer(settings.save_redaction_rules, project_id, rules, self._clock())

    async def get_ingest_settings(self, project_id: str) -> IngestSettings:
        return await self._on_read_pool(settings.get_ingest_settings, project_id)

    async def list_saved_filters(self, project_id: str) -> list[SavedFilter]:
        return await self._on_read_pool(settings.list_saved_filters, project_id)

    async def save_filter(self, f: NewSavedFilter) -> SavedFilter:
        return await self._on_writer(settings.save_filter, f, self._clock())

    async def delete_filter(self, filter_id: str) -> None:
        await self._on_writer(settings.delete_filter, filter_id)

    # ── derived-view maintenance (P2-08.3) ───────────────────────────────────────────────────

    async def finalize_stale_runs(self, older_than_ns: int, limit: int) -> list[RunKey]:
        """Q12 (REQ-032.b): one transaction; Runs locked by another writer are skipped."""
        return await self._on_writer(
            maintenance.finalize_stale_runs, older_than_ns, limit, self._clock()
        )

    async def rebuild(
        self,
        scope: RebuildScope,
        deriver: SpanDeriver,
        pricing: PricingVersion,
        *,
        batch_runs: int = 200,
    ) -> RebuildReport:
        """REQ-042: batches of ``batch_runs`` Runs, one transaction each, so ingest continues."""
        report, after = RebuildReport(), None
        while True:
            step, after = await self._on_writer(
                maintenance.rebuild_batch, scope, deriver, pricing, after, batch_runs,
                self._clock(), self.finalization_window_ns,
            )  # fmt: skip
            report += step
            if after is None:
                break
        await self._on_writer(maintenance.update_mapping_version_min)
        return report

    async def recompute_costs(
        self,
        project_id: str,
        time_range: TimeRange,
        pricing: PricingVersion,
        *,
        batch_runs: int = 200,
    ) -> int:
        """REQ-062.a: re-price the Project's LLM calls in the range; returns calls updated."""
        project_pk = await self._on_read_pool(admin._project_pk, project_id)
        total, after = 0, None
        while True:
            n, after = await self._on_writer(
                maintenance.recompute_costs_batch, project_pk, time_range, pricing, after,
                batch_runs, self._clock(), self.finalization_window_ns,
            )  # fmt: skip
            total += n
            if after is None:
                return total

    # ── retention (P2-09.1) ──────────────────────────────────────────────────────────────────

    async def _purge(
        self, trigger: PurgeTrigger, run: Callable[[dict[str, int]], Awaitable[None]]
    ) -> PurgeReport:
        started = self._clock()
        counts = {"age": 0, "size": 0, "bytes": 0}
        error: str | None = None
        try:
            await run(counts)
            # a concurrent PG writer may reuse an orphan resource row: skip, retry next purge
            with contextlib.suppress(IntegrityError):
                await self._on_writer(retention.cleanup, self._clock())
        except Exception as exc:
            error = type(exc).__name__  # class name only (Constitution Art. 3)
            raise
        finally:
            report = PurgeReport(
                trigger=trigger, started_ns=started, finished_ns=self._clock(),
                deleted_traces_age=counts["age"], deleted_traces_size=counts["size"],
                deleted_bytes_est=counts["bytes"], error_class=error,
            )  # fmt: skip
            await self._on_writer(retention.record_purge, report)
        return report

    async def purge_by_age(
        self, now_ns: int, batch_traces: int, *, trigger: PurgeTrigger = "scheduled"
    ) -> PurgeReport:
        """REQ-090.b: whole traces past their Project's maximum age, in bounded batches."""

        async def run(counts: dict[str, int]) -> None:
            while True:
                n, freed = await self._on_writer(retention.age_batch, now_ns, batch_traces)
                counts["age"] += n
                counts["bytes"] += freed
                if n < batch_traces:
                    return

        return await self._purge(trigger, run)

    async def purge_by_size(
        self, limit_bytes: int, batch_traces: int, *, trigger: PurgeTrigger = "scheduled"
    ) -> PurgeReport:
        """REQ-091.a: oldest traces across Projects until usage <= 90 % of ``limit_bytes``."""
        target = limit_bytes * 9 // 10

        async def run(counts: dict[str, int]) -> None:
            usage = await self._on_read_pool(retention.estimated_bytes)
            if usage <= limit_bytes:
                return
            while usage > target:
                n, freed = await self._on_writer(
                    retention.size_batch, usage - target, batch_traces, self._clock()
                )
                if n == 0:
                    return
                counts["size"] += n
                counts["bytes"] += freed
                usage -= freed

        return await self._purge(trigger, run)

    async def storage_usage(self) -> StorageUsage:
        return await self._on_read_pool(retention.storage_usage)

    async def last_purge(self) -> PurgeReport | None:
        return await self._on_read_pool(retention.last_purge)

    def job_lock(self, name: JobName) -> AbstractAsyncContextManager[bool]:
        """REQ-092.b: ``True`` when this holder got the lock; a second holder gets ``False``.

        In-process lock here (SQLite has a single writing process, DD-003); the PostgreSQL
        backend overrides it with a session advisory lock shared by every process.
        """
        return self._local_job_lock(name)

    @asynccontextmanager
    async def _local_job_lock(self, name: str) -> AsyncIterator[bool]:
        locks: dict[str, asyncio.Lock] = self.__dict__.setdefault("_job_locks", {})
        lock = locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            yield False
            return
        async with lock:
            yield True
