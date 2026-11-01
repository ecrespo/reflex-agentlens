"""Kind mapping and span labels (plan task P4-01.1; API §6.8, Analyze A-10, REQ-150.a).

One table between storage values (``spans.category``, ``graph_nodes.node_kind``) and the DTO
kinds, used by the span tree, the graph view and the UI colors.
"""

from __future__ import annotations

from typing import Final, Literal

from reflex_agentlens.query.dto import SpanKind
from reflex_agentlens.store.records import LLMCallRow, SpanCategory, ToolCallRow

__all__ = [
    "GraphKind",
    "format_tokens",
    "graph_node_kind",
    "span_is_genai",
    "span_kind",
    "span_label",
]

GraphKind = Literal["agent", "node", "tool", "llm", "approval"]

_SPAN_KIND: Final[dict[str, SpanKind]] = {
    "llm": "llm",
    "tool": "tool",
    "agent": "agent",
    "approval": "approval",
    "generic_genai": "generic",
    "generic": "generic",
}
_NODE_KIND: Final[dict[str, GraphKind]] = {"agent": "agent", "workflow": "agent", "node": "node"}


def span_kind(category: SpanCategory | str) -> SpanKind:
    """API §6.8: ``generic_genai`` → ``generic``; unknown values fall back to ``generic``."""
    return _SPAN_KIND.get(category, "generic")


def span_is_genai(category: SpanCategory | str) -> bool:
    """The GenAI badge: every category but plain ``generic``."""
    return category in _SPAN_KIND and category != "generic"


def graph_node_kind(node_kind: str, span_category: SpanCategory | str | None = None) -> GraphKind:
    """API §6.8: ``workflow`` → ``agent``; a node on an approval span is an ``approval`` node."""
    if span_category == "approval":
        return "approval"
    return _NODE_KIND.get(node_kind, "node")


def format_tokens(n: int) -> str:
    """Compact token counts for labels: ``999``, ``1.2k``, ``3.4M``."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def span_label(
    name: str,
    category: SpanCategory | str,
    *,
    llm: LLMCallRow | None = None,
    tool: ToolCallRow | None = None,
    agent_name: str | None = None,
) -> str:
    """REQ-150.a: ``chat gpt-4o · 1.2k tok``, ``execute_tool search_web``, ``invoke_agent x``."""
    if llm is not None:
        label = f"{llm.operation} {llm.model}"
        tokens = (llm.input_tokens or 0) + (llm.output_tokens or 0)
        return f"{label} · {format_tokens(tokens)} tok" if tokens else label
    if tool is not None:
        return f"execute_tool {tool.tool_name}"
    if category == "agent" and agent_name:
        return f"invoke_agent {agent_name}"
    return name
