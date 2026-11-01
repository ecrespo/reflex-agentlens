"""Mermaid graph text and replay (plan tasks P6-07.1, P6-07.2; REQ-153, 154, DD-011)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from reflex_agentlens.query.dto import AgentGraph, GraphEdge, GraphNode
from reflex_agentlens.views.graph import (
    escape_label,
    graph_text,
    node_of_span,
    replay_delay_ms,
    replay_order,
    replay_step,
)
from tests.unit.ui.factories import MS, span

HOSTILE = [
    'a"] --> evil["x',
    "x]-->y[z",
    "%% comment\n%%{init: {'securityLevel':'loose'}}%%",
    "<img src=x onerror=alert(1)>",
    "click n0 call alert()",
    "line\nbreak; style n0 fill:red",
    "a|b|c `code` {d} (e) #f &g;",
]


def node(i: int, label: str = "plan", **kw: Any) -> GraphNode:
    base: dict[str, Any] = {
        "id": f"id{i}",
        "label": label,
        "kind": "agent",
        "executions": 1,
        "status": "ok",
        "interrupted": False,
        "span_ids": [f"{i:016x}"],
    }
    base.update(kw)
    return GraphNode(**base)


def graph(*nodes: GraphNode, edges: list[GraphEdge] | None = None, **kw: Any) -> AgentGraph:
    return AgentGraph(
        nodes=list(nodes),
        edges=edges or [],
        collapsed=kw.get("collapsed", False),
        fallback_sequence=kw.get("fallback", False),
        total_executions=len(nodes),
    )


NODE_LINE = re.compile(
    r'^  n\d+(\(\["|\["|\[/"|\{"|\(")[A-Za-z0-9 _.,:/+=?@#;]*("\]\)|"\]|"/\]|"\}|"\))$'
)


@pytest.mark.parametrize("label", HOSTILE)
def test_hostile_labels_stay_inside_their_node_req_153(label: str) -> None:
    text = graph_text(graph(node(0, label), node(1, kind="tool"))).text
    lines = text.splitlines()
    node_lines = [ln for ln in lines if re.match(r"^  n\d+[\[\(\{]", ln)]
    assert len(node_lines) == 2
    for line in node_lines:
        assert NODE_LINE.match(line), line
    assert "%%" not in text
    assert "<" not in text
    assert not any(ln.startswith("  click") or ln.startswith("  style") for ln in lines)


def test_escape_uses_entity_codes_and_caps_length() -> None:
    assert escape_label('a"b') == "a#34;b"
    assert escape_label("x" * 100) == "x" * 59 + "#8230;"  # cut to 60 with an ellipsis


def test_shapes_classes_edges_and_stable_ids_req_153() -> None:
    g = graph(
        node(0, "plan", executions=3),
        node(1, "search", kind="tool", status="error"),
        node(2, "ok?", kind="approval"),
        edges=[
            GraphEdge(source="id0", target="id1", kind="parent", count=2),
            GraphEdge(source="id1", target="id2", kind="sequence", count=1),
            GraphEdge(source="id0", target="id2", kind="sequence", count=1, taken=False),
            GraphEdge(source="id0", target="gone", kind="parent", count=1),
        ],
    )
    out = graph_text(g, current_span=f"{1:016x}")
    lines = out.text.splitlines()
    assert lines[0] == "flowchart LR"
    assert '  n0(["plan #215;3"])' in lines
    assert "  class n0 taken,retry" in lines
    assert "  class n1 taken,error,current" in lines
    assert "  class n2 taken,approval" in lines
    assert "  n0 -->|2x| n1" in lines
    assert "  n1 ==> n2" in lines
    assert "  n0 -.-> n2" in lines
    assert not any("gone" in ln for ln in lines)  # edges to unknown nodes are dropped
    assert graph_text(g).text.replace(",current", "") == out.text.replace(",current", "")
    assert node_of_span(out, f"{2:016x}") == "n2"
    assert node_of_span(out, "f" * 16) is None


def test_collapse_and_fallback_notes() -> None:
    assert graph_text(graph(node(0))).note == ""
    note = graph_text(graph(node(0), collapsed=True, fallback=True)).note
    assert "collapsed" in note
    assert "call sequence" in note


def test_replay_steps_in_start_order_req_154() -> None:
    spans = [
        span("c", None, 0, 30, 40),
        span("a", None, 0, 10, 20),
        span("b", None, 0, 10, 15),
        span("p", None, 0, 0, 0, placeholder=True),
    ]
    order = replay_order(spans)
    assert [s[0] for s in order] == ["a", "b", "c"]
    assert replay_step(order, None, 1) == order[0]
    assert replay_step(order, order[0], 1) == order[1]
    assert replay_step(order, order[2], 1) == order[2]  # clamped at the end
    assert replay_step(order, order[0], -1) == order[0]
    assert replay_step(order, None, -1) == order[2]
    assert replay_step([], None, 1) is None


def test_replay_delays() -> None:
    assert replay_delay_ms("1", 1000 * MS) == 1000
    assert replay_delay_ms("2", 1000 * MS) == 500
    assert replay_delay_ms("5", 0) == 150  # never faster than the floor
    assert replay_delay_ms("1", 3_600_000 * MS) == 3000  # long gaps are capped
    assert replay_delay_ms("step", 1000 * MS) is None
