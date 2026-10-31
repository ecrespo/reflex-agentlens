"""Async read surface shared by both SQL backends (plan tasks P2-07.1 … P2-07.4).

Each method runs one synchronous query function from ``reads``, ``sessions``, ``aggregates`` or
``metrics`` on the backend's read pool (``read_engine``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncEngine

from reflex_agentlens.store import aggregates, metrics, reads, sessions
from reflex_agentlens.store.records import (
    AggQuery,
    ApprovalStats,
    FacetField,
    Kpis,
    LatencyDim,
    LatencyRow,
    LLMCallView,
    ModelUsage,
    Page,
    ProjectScope,
    RunCursor,
    RunKey,
    RunQuery,
    RunSummary,
    SessionCursor,
    SessionDetail,
    SessionQuery,
    SessionSummary,
    SourceUsageRow,
    SourceView,
    SpanRaw,
    TimeRange,
    TimeSeries,
    ToolStatRow,
    TraceDetail,
)

__all__ = ["ReadMethods"]

_R = TypeVar("_R")


class ReadMethods:
    """Mixin: the read and aggregate methods of ``StorageBackend``."""

    @property
    def read_engine(self) -> AsyncEngine:  # pragma: no cover - overridden by every backend
        raise NotImplementedError

    async def _on_read_pool(self, fn: Callable[..., _R], *args: Any) -> _R:
        async with self.read_engine.connect() as conn:
            return await conn.run_sync(fn, *args)

    # ── runs and traces (P2-07.1) ────────────────────────────────────────────────────────────

    async def list_runs(self, query: RunQuery) -> Page[RunSummary, RunCursor]:
        return await self._on_read_pool(reads.list_runs, query)

    async def find_runs_by_trace_id(self, scope: ProjectScope, trace_id: bytes) -> list[RunKey]:
        return await self._on_read_pool(reads.find_runs_by_trace_id, scope, trace_id)

    async def get_trace(self, key: RunKey) -> TraceDetail | None:
        return await self._on_read_pool(reads.get_trace, key)

    async def get_span_raw(self, key: RunKey, span_id: bytes) -> SpanRaw | None:
        return await self._on_read_pool(reads.get_span_raw, key, span_id)

    async def list_llm_calls(self, key: RunKey) -> list[LLMCallView]:
        return await self._on_read_pool(reads.list_llm_calls, key)

    async def list_sources(self, scope: ProjectScope) -> list[SourceView]:
        return await self._on_read_pool(reads.list_sources, scope)

    async def distinct_values(
        self, scope: ProjectScope, field: FacetField, time_range: TimeRange, limit: int = 100
    ) -> list[str]:
        return await self._on_read_pool(reads.distinct_values, scope, field, time_range, limit)

    # ── Sessions (P2-07.2) ───────────────────────────────────────────────────────────────────

    async def list_sessions(self, query: SessionQuery) -> Page[SessionSummary, SessionCursor]:
        return await self._on_read_pool(sessions.list_sessions, query)

    async def get_session(self, scope: ProjectScope, session_id: str) -> SessionDetail | None:
        return await self._on_read_pool(sessions.get_session, scope, session_id)

    # ── aggregates (P2-07.3, P2-07.4) ────────────────────────────────────────────────────────

    async def agg_kpis(self, q: AggQuery) -> Kpis:
        return await self._on_read_pool(aggregates.agg_kpis, q)

    async def agg_runs_over_time(self, q: AggQuery) -> TimeSeries:
        return await self._on_read_pool(aggregates.agg_runs_over_time, q)

    async def agg_tokens_cost_by_model(self, q: AggQuery) -> ModelUsage:
        return await self._on_read_pool(aggregates.agg_tokens_cost_by_model, q)

    async def agg_latency_percentiles(self, q: AggQuery, by: LatencyDim) -> list[LatencyRow]:
        return await self._on_read_pool(aggregates.agg_latency_percentiles, q, by)

    async def agg_tool_stats(self, q: AggQuery, top_n: int) -> list[ToolStatRow]:
        return await self._on_read_pool(aggregates.agg_tool_stats, q, top_n)

    async def agg_by_source(self, q: AggQuery) -> list[SourceUsageRow]:
        return await self._on_read_pool(aggregates.agg_by_source, q)

    async def agg_approvals(self, q: AggQuery) -> ApprovalStats:
        return await self._on_read_pool(aggregates.agg_approvals, q)

    async def agg_metric_token_usage(self, q: AggQuery) -> TimeSeries:
        return await self._on_read_pool(metrics.agg_metric_token_usage, q)
