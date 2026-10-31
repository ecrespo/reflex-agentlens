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
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from reflex_agentlens.store.records import (
    FacetField,
    LLMCallView,
    NormalizedBatch,
    Page,
    ProjectScope,
    RunKey,
    RunQuery,
    RunSummary,
    SchemaStatus,
    SourceView,
    SpanRaw,
    TimeRange,
    TraceDetail,
    WriteResult,
)

__all__ = ["StorageBackend"]


@runtime_checkable
class StorageBackend(Protocol):
    """A storage backend. Every implementation passes ``tests/contract/`` (REQ-070.b)."""

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

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

    # ── reads: runs and traces (P2-07.1) ─────────────────────────────────────────────────────

    async def list_runs(self, query: RunQuery) -> Page[RunSummary]:
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
