"""DTO builders for the view-model tests (P6)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from reflex_agentlens.query.dto import RunSummary, SourceRef, SpanNode

T0 = 1_793_500_000_000_000_000  # 2026-11-01T02:26:40Z
MS = 1_000_000


def run(**kw: Any) -> RunSummary:
    base: dict[str, Any] = {
        "project_id": "01PROJECT0000000000000000A",
        "trace_id": "ab" * 16,
        "start_ns": T0,
        "end_ns": T0 + 1000 * MS,
        "duration_ns": 1000 * MS,
        "last_span_ns": T0 + 1000 * MS,
        "status": "ok",
        "root_name": "invoke_agent support",
        "agent_name": "support",
        "source": SourceRef(service_name="api", service_namespace="shop"),
        "source_count": 1,
        "models": ["gpt-4o"],
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_input_tokens": 0,
        "total_tokens": 120,
        "usage_source": "llm",
        "cost_usd": Decimal("0.0123"),
        "unpriced": False,
        "llm_calls": 1,
        "tool_calls": 0,
        "error_spans": 0,
        "span_count": 2,
        "missing_parents": 0,
        "session_id": None,
        "is_genai": True,
    }
    base.update(kw)
    return RunSummary(**base)


def span(
    span_id: str, parent: str | None, depth: int, start_ms: int, end_ms: int, **kw: Any
) -> SpanNode:
    base: dict[str, Any] = {
        "span_id": span_id.ljust(16, "0"),
        "parent_span_id": parent.ljust(16, "0") if parent else None,
        "depth": depth,
        "missing_parent": False,
        "name": f"span {span_id}",
        "label": f"span {span_id}",
        "kind": "generic",
        "is_genai": False,
        "start_ns": T0 + start_ms * MS,
        "end_ns": T0 + end_ms * MS,
        "duration_ns": (end_ms - start_ms) * MS,
        "status": "ok",
        "status_message": None,
        "source": None,
        "agent_name": None,
        "llm": None,
        "tool": None,
        "approval": None,
        "subtree_tokens": 0,
        "subtree_cost_usd": Decimal(0),
        "subtree_includes_unpriced": False,
        "truncated": False,
        "redacted": False,
        "content_dropped": False,
    }
    base.update(kw)
    return SpanNode(**base)
