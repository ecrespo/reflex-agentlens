"""The ``StorageBackend`` protocol (API §5 v1.2; plan task P2-03.1; REQ-070.a).

The protocol is async and fully typed. It grows **with the method-ownership table** of the P2
Tasks (Analyze A-06): a method is declared here by the task that implements and contract-tests
it, together with its record types, so the protocol never names a type or a method that no
backend provides yet. The authoritative full list remains API §5.

Implemented so far:

- P2-05.1 / P2-06.1: ``open``, ``close``, ``ping``
- P2-02.2: ``schema_status``, ``migrate``
- P2-05.2 / P2-06.1: ``write_batch``
- P2-07.1: ``list_runs``, ``find_runs_by_trace_id``, ``get_trace``, ``get_span_raw``,
  ``list_llm_calls``, ``list_sources``, ``distinct_values``
- P2-07.2: ``list_sessions``, ``get_session``
- P2-07.3: ``agg_kpis``, ``agg_runs_over_time``, ``agg_tokens_cost_by_model``,
  ``agg_latency_percentiles``, ``agg_tool_stats``, ``agg_by_source``, ``agg_approvals``
- P2-07.4: ``write_metric_points``, ``agg_metric_token_usage``
- P2-08.2: ``save_pricing``, ``list_pricing_versions``, ``get_pricing``, ``list_unpriced_models``,
  ``save_redaction_rules``, ``get_ingest_settings``, ``list_saved_filters``, ``save_filter``,
  ``delete_filter``
- P4-05.2: ``get_pricing_version``
- P2-08.3: ``finalize_stale_runs``, ``rebuild``, ``recompute_costs``
- P2-09.1: ``purge_by_age``, ``purge_by_size``, ``storage_usage``, ``last_purge``, ``job_lock``
- P5-03.2: ``count_purgeable``
- P2-10.1 / P2-10.2: ``changes`` (the :class:`ChangeFeed`), ``start_change_listener``
- P2-08.1: ``list_projects``, ``create_project``, ``update_project``, ``delete_project``,
  ``create_api_key``, ``list_api_keys``, ``revoke_api_key``, ``resolve_api_key``, ``touch_api_key``
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable

from reflex_agentlens.store.changefeed import ChangeFeed
from reflex_agentlens.store.records import (
    AggQuery,
    ApiKeyRecord,
    ApprovalStats,
    BatchRejected,
    FacetField,
    IngestSettings,
    JobName,
    Kpis,
    LatencyDim,
    LatencyRow,
    LLMCallView,
    MetricPoint,
    ModelUsage,
    NewApiKeyRecord,
    NewProject,
    NewSavedFilter,
    NormalizedBatch,
    Page,
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
    RunCursor,
    RunKey,
    RunQuery,
    RunSummary,
    SavedFilter,
    SchemaStatus,
    SecretName,
    SessionCursor,
    SessionDetail,
    SessionQuery,
    SessionSummary,
    SourceUsageRow,
    SourceView,
    SpanDeriver,
    SpanRaw,
    StorageUsage,
    TimeRange,
    TimeSeries,
    ToolStatRow,
    TraceDetail,
    UnpricedModel,
    WriteResult,
)

__all__ = ["StorageBackend"]


@runtime_checkable
class StorageBackend(Protocol):
    """A storage backend. Every implementation passes ``tests/contract/`` (REQ-070.b)."""

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

    changes: ChangeFeed
    """API §5.2: events published after each commit; lossy, subscribers re-query."""

    async def start_change_listener(self) -> None:
        """DD-007: receive other processes' events (PostgreSQL LISTEN); no-op on SQLite."""

    async def open(self) -> None:
        """Connect pools and check the database version. Never migrates implicitly."""

    async def close(self) -> None:
        """Release every connection; idempotent."""

    async def ping(self) -> bool:
        """``True`` when the database answers (REQ-330, REQ-340); never raises."""

    async def schema_status(self) -> SchemaStatus:
        """``CURRENT``, ``OUTDATED`` or ``NEWER`` (REQ-074)."""

    async def migrate(self) -> None:
        """Alembic upgrade to head, under a lock where several processes may race (REQ-074.d)."""

    # ── ingest path ──────────────────────────────────────────────────────────────────────────

    async def write_batch(self, batch: NormalizedBatch) -> WriteResult:
        """Persist one normalized batch in one transaction (API §5.1)."""

    async def write_group(
        self, batches: Sequence[NormalizedBatch]
    ) -> list[WriteResult | BatchRejected]:
        """Several batches in one transaction, one SAVEPOINT each (API v1.3 §5.1, DD-003).

        A batch hitting a data error is rolled back alone and returned as ``BatchRejected``;
        any other error fails the call. Events are published only for committed batches.
        """

    async def get_or_create_secret(self, name: SecretName) -> bytes:
        """A random secret in ``app_meta``, the same for every process (Data Model §2.1)."""

    async def write_metric_points(self, points: Sequence[MetricPoint]) -> int:
        """Insert GenAI metric points idempotently; returns how many were new (REQ-005)."""

    async def resolve_api_key(self, key_id: str) -> ApiKeyRecord | None:
        """Q14: the key record (salt and hash only) by public key ID, revoked or not (REQ-006)."""

    async def touch_api_key(self, key_id: str, at_ns: int) -> None:
        """Record use of a key, at most once per minute."""

    # ── reads: runs and traces (P2-07.1) ─────────────────────────────────────────────────────

    async def list_runs(self, query: RunQuery) -> Page[RunSummary, RunCursor]:
        """Q1-Q3: filtered, keyset-paginated Runs (REQ-100, REQ-101); empty scope → empty."""

    async def find_runs_by_trace_id(self, scope: ProjectScope, trace_id: bytes) -> list[RunKey]:
        """Q4: every Run with this trace ID inside the scope (REQ-103.d)."""

    async def get_trace(self, key: RunKey) -> TraceDetail | None:
        """Q5: the Run and its spans ordered by (start, span_id) (REQ-103.a)."""

    async def get_span_raw(self, key: RunKey, span_id: bytes) -> SpanRaw | None:
        """Q5b: raw attributes, resource, events and links of one span (REQ-151.e)."""

    async def list_llm_calls(self, key: RunKey) -> list[LLMCallView]:
        """LLM calls of one Run with raw attributes/events for message rendering (REQ-104)."""

    async def list_sources(self, scope: ProjectScope) -> list[SourceView]:
        """Sources of the scoped Projects (REQ-031.c)."""

    async def distinct_values(
        self, scope: ProjectScope, field: FacetField, time_range: TimeRange, limit: int = 100
    ) -> list[str]:
        """Sorted distinct facet values for filter pickers (REQ-101)."""

    # ── reads: Sessions (P2-07.2) ────────────────────────────────────────────────────────────

    async def list_sessions(self, query: SessionQuery) -> Page[SessionSummary, SessionCursor]:
        """Q9a: Sessions with at least one Run, filtered, sorted, keyset-paginated (REQ-105.a)."""

    async def get_session(self, scope: ProjectScope, session_id: str) -> SessionDetail | None:
        """Q9b: the Session and its Runs in start order with running totals (REQ-105.b)."""

    # ── aggregates (P2-07.3, P2-07.4; SQL only, bounded rows, REQ-106.h) ─────────────────────

    async def agg_kpis(self, q: AggQuery) -> Kpis:
        """Q8 (REQ-106.g)."""

    async def agg_runs_over_time(self, q: AggQuery) -> TimeSeries:
        """Q10a (REQ-106.a): Runs per bucket and status."""

    async def agg_tokens_cost_by_model(self, q: AggQuery) -> ModelUsage:
        """Q10b (REQ-106.b)."""

    async def agg_latency_percentiles(self, q: AggQuery, by: LatencyDim) -> list[LatencyRow]:
        """Q10c (REQ-106.c, DD-005)."""

    async def agg_tool_stats(self, q: AggQuery, top_n: int) -> list[ToolStatRow]:
        """Q10d (REQ-106.d)."""

    async def agg_by_source(self, q: AggQuery) -> list[SourceUsageRow]:
        """Q10e (REQ-106.e)."""

    async def agg_approvals(self, q: AggQuery) -> ApprovalStats:
        """Q10f (REQ-106.f)."""

    async def agg_metric_token_usage(self, q: AggQuery) -> TimeSeries:
        """Q11 (REQ-107.a): tokens per bucket, model and token type."""

    # ── settings: Projects and API keys (P2-08.1) ────────────────────────────────────────────

    async def list_projects(self) -> list[Project]:
        """Every Project, by slug."""

    async def create_project(self, new: NewProject) -> Project:
        """REQ-170.a; ``Conflict`` on a duplicate slug."""

    async def update_project(self, project_id: str, patch: ProjectPatch) -> Project:
        """Apply a patch and bump ``settings_version`` (REQ-173, REQ-006.c)."""

    async def delete_project(self, project_id: str) -> None:
        """REQ-170.d: cascades to all of the Project's data."""

    async def create_api_key(self, rec: NewApiKeyRecord) -> ApiKeyRecord:
        """REQ-170.b: salt and hash only (REQ-320.a)."""

    async def list_api_keys(self, project_id: str) -> list[ApiKeyRecord]:
        """Keys of one Project, oldest first."""

    async def revoke_api_key(self, key_id: str, at_ns: int) -> None:
        """REQ-170.c: terminal."""

    # ── settings: pricing, redaction, ingest settings, saved filters (P2-08.2) ────────────────

    async def save_pricing(
        self,
        entries: Sequence[PricingEntry],
        author: str | None,
        *,
        effective_from_ns: int | None = None,
        source: PricingSource = "editor",
        note: str | None = None,
    ) -> PricingVersion:
        """REQ-060.a/c: a new immutable version (``max + 1``); earlier versions never change."""

    async def list_pricing_versions(self) -> list[PricingVersionMeta]:
        """Every pricing version, oldest first."""

    async def get_pricing(self, at_ns: int | None = None) -> PricingVersion:
        """The version in effect at ``at_ns`` (default now); ``NotFound`` if none (REQ-060.c)."""

    async def get_pricing_version(self, version: int) -> PricingVersion:
        """One version by number; ``NotFound`` if it does not exist (REQ-171.a history)."""

    async def list_unpriced_models(self, scope: ProjectScope) -> list[UnpricedModel]:
        """Models of unpriced LLM calls, most calls first (REQ-171.b)."""

    async def save_redaction_rules(self, project_id: str, rules: RedactionRuleSet) -> None:
        """Replace the Project's rule set and bump ``settings_version`` (REQ-172)."""

    async def get_ingest_settings(self, project_id: str) -> IngestSettings:
        """What ingest caches per Project (REQ-006.c)."""

    async def list_saved_filters(self, project_id: str) -> list[SavedFilter]:
        """Saved filters of a Project, by name (REQ-102.a)."""

    async def save_filter(self, f: NewSavedFilter) -> SavedFilter:
        """Create or update by ``id``; ``Conflict`` on a duplicate name (REQ-102.a)."""

    async def delete_filter(self, project_id: str, filter_id: str) -> None:
        """``NotFound`` when the Project has no such filter (another Project's ID included)."""

    # ── derived views (P2-08.3) ──────────────────────────────────────────────────────────────

    async def finalize_stale_runs(self, older_than_ns: int, limit: int) -> list[RunKey]:
        """Q12: ``in_progress`` Runs silent since before ``older_than_ns`` → ``incomplete``."""

    async def rebuild(
        self,
        scope: RebuildScope,
        deriver: SpanDeriver,
        pricing: PricingVersion,
        *,
        batch_runs: int = 200,
    ) -> RebuildReport:
        """REQ-042: re-derive spans and recompute Runs/Sessions in bounded batches."""

    async def recompute_costs(
        self,
        project_id: str,
        time_range: TimeRange,
        pricing: PricingVersion,
        *,
        batch_runs: int = 200,
    ) -> int:
        """REQ-062.a: re-price LLM calls with ``pricing``; Run and Session costs follow."""

    # ── retention and coordination (P2-09.1) ─────────────────────────────────────────────────

    async def purge_by_age(
        self, now_ns: int, batch_traces: int, *, trigger: PurgeTrigger = "scheduled"
    ) -> PurgeReport:
        """REQ-090.b: delete whole traces past their Project's maximum age; logged."""

    async def purge_by_size(
        self, limit_bytes: int, batch_traces: int, *, trigger: PurgeTrigger = "scheduled"
    ) -> PurgeReport:
        """REQ-091.a: oldest traces across Projects until usage <= 90 % of the limit; logged."""

    async def count_purgeable(self, now_ns: int) -> int:
        """Traces past their Project's maximum age at ``now_ns``; nothing is deleted."""

    async def storage_usage(self) -> StorageUsage:
        """REQ-173.a."""

    async def last_purge(self) -> PurgeReport | None:
        """REQ-173.b."""

    def job_lock(self, name: JobName) -> AbstractAsyncContextManager[bool]:
        """REQ-092.b: ``async with backend.job_lock("retention") as acquired: ...``."""
        ...
