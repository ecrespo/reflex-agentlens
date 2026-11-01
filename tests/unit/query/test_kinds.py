"""Kind mapping and labels (plan task P4-01.1; API §6.8, Analyze A-10, REQ-150.a)."""

from __future__ import annotations

import pytest

from reflex_agentlens.query.kinds import (
    format_tokens,
    graph_node_kind,
    span_is_genai,
    span_kind,
    span_label,
)
from reflex_agentlens.store.records import LLMCallRow, ToolCallRow


@pytest.mark.parametrize(
    ("category", "kind", "genai"),
    [
        ("llm", "llm", True),
        ("tool", "tool", True),
        ("agent", "agent", True),
        ("approval", "approval", True),
        ("generic_genai", "generic", True),
        ("generic", "generic", False),
        ("future_value", "generic", False),
    ],
)
def test_span_category_mapping(category: str, kind: str, genai: bool) -> None:
    """API §6.8 span rows: ``generic_genai`` keeps the GenAI badge with the generic color."""
    assert span_kind(category) == kind
    assert span_is_genai(category) is genai


@pytest.mark.parametrize(
    ("node_kind", "category", "expected"),
    [
        ("agent", "agent", "agent"),
        ("workflow", "agent", "agent"),
        ("node", "generic", "node"),
        ("node", None, "node"),
        ("node", "approval", "approval"),
        ("workflow", "approval", "approval"),
        ("other", None, "node"),
    ],
)
def test_graph_node_kind_mapping(node_kind: str, category: str | None, expected: str) -> None:
    """API §6.8 graph rows: ``workflow`` → ``agent``; approval spans override."""
    assert graph_node_kind(node_kind, category) == expected


@pytest.mark.parametrize(
    ("n", "text"),
    [(0, "0"), (999, "999"), (1000, "1k"), (1234, "1.2k"), (1_500_000, "1.5M"), (2_000_000, "2M")],
)
def test_format_tokens(n: int, text: str) -> None:
    assert format_tokens(n) == text


def test_labels_req_150a() -> None:
    """REQ-150.a: ``chat gpt-4o · 1.2k tok``, ``execute_tool x``, ``invoke_agent y``."""
    llm = LLMCallRow(
        start_ns=0, duration_ns=1, operation="chat", unpriced=False, model="gpt-4o",
        input_tokens=1000, output_tokens=234,
    )  # fmt: skip
    assert span_label("span", "llm", llm=llm) == "chat gpt-4o · 1.2k tok"
    no_tokens = LLMCallRow(start_ns=0, duration_ns=1, operation="chat", unpriced=True)
    assert span_label("span", "llm", llm=no_tokens) == "chat unknown"
    tool = ToolCallRow(start_ns=0, duration_ns=1, status="ok", tool_name="search_web")
    assert span_label("span", "tool", tool=tool) == "execute_tool search_web"
    assert span_label("agent run", "agent", agent_name="planner") == "invoke_agent planner"
    assert span_label("agent run", "agent") == "agent run"
    assert span_label("GET /x", "generic") == "GET /x"
