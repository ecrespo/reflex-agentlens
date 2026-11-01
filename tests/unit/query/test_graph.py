"""Agent graph (plan task P4-04.1; REQ-153.a-e, DD-011, API §6.8)."""

from __future__ import annotations

from reflex_agentlens.query.graph import MAX_NODES, build_graph
from reflex_agentlens.store import records as r

SRC = r.SourceKey("svc")


def sid(n: int) -> bytes:
    return n.to_bytes(8, "big")


def span(
    n: int,
    parent: int | None,
    start: int,
    *,
    node: tuple[str, str] | None = None,  # (node_name, node_kind)
    category: str = "generic",
    error: bool = False,
    approval: bool = False,
    llm: str | None = None,
    tool: str | None = None,
) -> r.TraceSpan:
    return r.TraceSpan(
        span_id=sid(n),
        parent_span_id=None if parent is None else sid(parent),
        name=f"s{n}",
        category=category,  # type: ignore[arg-type]
        otel_kind=0,
        start_ns=start,
        end_ns=start + 5,
        status_code=2 if error else 0,
        status_message=None,
        flags=0,
        source=SRC,
        agent_name=None,
        llm_call=None
        if llm is None
        else r.LLMCallRow(
            start_ns=start, duration_ns=1, operation="chat", unpriced=True, model=llm
        ),
        tool_call=None
        if tool is None
        else r.ToolCallRow(start_ns=start, duration_ns=1, status="ok", tool_name=tool),
        approval=None
        if not approval
        else r.ApprovalRow(kind="approval", decision="approved", requested_ns=start),
        graph_node=None
        if node is None
        else r.GraphNodeRow(
            node_key=node[0],
            node_name=node[0],
            node_kind=node[1],  # type: ignore[arg-type]
            start_ns=start,
            end_ns=start + 5,
            status="error" if error else "ok",
        ),
    )


def edges(g: object) -> set[tuple[str, str, str, int]]:
    labels = {n.id: n.label for n in g.nodes}  # type: ignore[attr-defined]
    return {(labels[e.source], labels[e.target], e.kind, e.count) for e in g.edges}  # type: ignore[attr-defined]


def test_langgraph_retries_merge_with_execution_count_req_153b() -> None:
    spans = [
        span(1, None, 0, node=("graph", "workflow"), category="agent"),
        span(2, 1, 1, node=("router", "node")),
        span(3, 1, 2, node=("act", "node"), error=True),
        span(4, 1, 3, node=("act", "node")),  # retry of act
        span(5, 1, 4, node=("respond", "node")),
        span(6, 3, 2, llm="gpt-4o"),  # a non-graph span inside act
    ]
    g = build_graph(spans)
    assert g is not None
    assert not g.fallback_sequence
    assert not g.collapsed
    by_label = {n.label: n for n in g.nodes}
    assert by_label["graph"].kind == "agent"  # workflow → agent (§6.8)
    assert by_label["act"].executions == 2
    assert by_label["act"].status == "error"
    assert by_label["act"].span_ids == [sid(3).hex(), sid(4).hex()]
    assert by_label["router"].executions == 1
    assert edges(g) == {
        ("graph", "router", "parent", 1),
        ("graph", "act", "parent", 2),
        ("graph", "respond", "parent", 1),
        ("router", "act", "sequence", 1),
        ("act", "respond", "sequence", 1),
    }
    assert g.total_executions == 5


def test_nested_agents_and_parent_through_plain_spans_req_153a() -> None:
    spans = [
        span(1, None, 0, node=("planner", "agent"), category="agent"),
        span(2, 1, 1),  # plain span between graph nodes
        span(3, 2, 2, node=("researcher", "agent"), category="agent"),
        span(4, 3, 3, node=("search", "node")),
    ]
    g = build_graph(spans)
    assert g is not None
    assert edges(g) == {
        ("planner", "researcher", "parent", 1),
        ("researcher", "search", "parent", 1),
    }


def test_approval_inside_a_node_marks_it_interrupted_req_153b() -> None:
    spans = [
        span(1, None, 0, node=("review", "node")),
        span(2, 1, 1, category="approval", approval=True),
        span(3, None, 5, node=("gate", "node"), category="approval", approval=True),
    ]
    g = build_graph(spans)
    assert g is not None
    by_label = {n.label: n for n in g.nodes}
    assert by_label["review"].interrupted
    assert by_label["review"].kind == "node"
    assert by_label["gate"].kind == "approval"  # approval span overrides (§6.8)


def test_more_than_150_executions_collapse_req_153d() -> None:
    spans = [span(1, None, 0, node=("loop", "workflow"))]
    for i in range(200):  # alternating a/b: no adjacent retries, 201 expanded nodes
        spans.append(span(10 + i, 1, 1 + i, node=("a" if i % 2 else "b", "node")))
    g = build_graph(spans)
    assert g is not None
    assert g.collapsed
    assert sorted(n.label for n in g.nodes) == ["a", "b", "loop"]
    assert {n.label: n.executions for n in g.nodes} == {"loop": 1, "a": 100, "b": 100}
    assert g.total_executions == 201
    full = build_graph(spans, full=True)
    assert full is not None
    assert not full.collapsed
    assert len(full.nodes) == 201 > MAX_NODES


def test_fallback_sequence_without_graph_nodes_req_153e() -> None:
    spans = [
        span(1, None, 0, category="agent"),
        span(2, 1, 1, llm="gpt-4o", category="llm"),
        span(3, 1, 2, tool="search_web", category="tool", error=True),
        span(4, 1, 3, llm="gpt-4o", category="llm"),
    ]
    g = build_graph(spans)
    assert g is not None
    assert g.fallback_sequence
    assert [(n.label, n.kind) for n in g.nodes] == [
        ("gpt-4o", "llm"),
        ("search_web", "tool"),
        ("gpt-4o", "llm"),
    ]
    assert g.nodes[1].status == "error"
    assert [(e.source, e.target, e.kind) for e in g.edges] == [
        ("n0", "n1", "sequence"),
        ("n1", "n2", "sequence"),
    ]


def test_fallback_collapses_above_150_calls() -> None:
    spans = [span(1, None, 0)] + [
        span(10 + i, 1, 1 + i, llm="m" if i % 2 else None, tool=None if i % 2 else "t")
        for i in range(200)
    ]
    g = build_graph(spans)
    assert g is not None
    assert g.collapsed
    assert {n.label: n.executions for n in g.nodes} == {"t": 100, "m": 100}


def test_no_graph_and_no_calls_is_none() -> None:
    assert build_graph([span(1, None, 0)]) is None
    assert build_graph([]) is None


def test_parent_cycle_does_not_hang() -> None:
    spans = [span(1, 2, 0, node=("a", "node")), span(2, 1, 1, node=("b", "node"))]
    g = build_graph(spans)
    assert g is not None
    assert len(g.nodes) == 2
