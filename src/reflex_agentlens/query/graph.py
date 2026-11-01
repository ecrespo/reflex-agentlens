"""Agent graph of a Run (plan task P4-04.1; REQ-153, Tech Design DD-011, API §6.7-6.8).

The graph shows the **executed** structure only. Graph nodes are the spans with a
``graph_nodes`` row. A node's parent is its nearest graph-node ancestor along the span parent
chain; ``parent`` edges link them, and ``sequence`` edges link consecutive sibling executions by
start time. Consecutive executions of the same node under the same parent are retries and merge
into one node with an execution count (REQ-153.b).

Above ``MAX_NODES`` nodes, every execution of the same node path (names from the root) merges
into one node and ``collapsed`` is set (REQ-153.d); ``full=True`` skips that. A Run without graph
nodes falls back to its LLM/tool calls in start order (``fallback_sequence``, REQ-153.e).
``AgentGraph`` is data: Mermaid text is produced by ``components``.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

from reflex_agentlens.core.ids import span_id_to_hex
from reflex_agentlens.query import dto
from reflex_agentlens.query.kinds import GraphKind, graph_node_kind
from reflex_agentlens.store import records as r

__all__ = ["MAX_NODES", "build_graph"]

MAX_NODES = 150


@dataclass(slots=True)
class _Exec:
    span: r.TraceSpan
    label: str
    kind: GraphKind
    parent: bytes | None  # span ID of the nearest graph-node ancestor
    path: tuple[str, ...] = ()


@dataclass(slots=True)
class _Node:
    id: str
    label: str
    kind: GraphKind
    spans: list[r.TraceSpan] = field(default_factory=list)
    error: bool = False
    interrupted: bool = False


def _key(sp: r.TraceSpan) -> tuple[int, bytes]:
    return (sp.start_ns, sp.span_id)


def _nearest(
    span_id: bytes | None, by_id: dict[bytes, r.TraceSpan], is_node: set[bytes]
) -> bytes | None:
    """The nearest graph-node span strictly above ``span_id`` (cycle-safe)."""
    seen: set[bytes] = set()
    current = by_id.get(span_id) if span_id is not None else None
    while current is not None and current.parent_span_id is not None:
        parent = current.parent_span_id
        if parent in seen or parent == current.span_id:
            return None
        seen.add(parent)
        if parent in is_node:
            return parent
        current = by_id.get(parent)
    return None


def _self_or_nearest(
    span_id: bytes, by_id: dict[bytes, r.TraceSpan], is_node: set[bytes]
) -> bytes | None:
    return span_id if span_id in is_node else _nearest(span_id, by_id, is_node)


def build_graph(spans: Sequence[r.TraceSpan], *, full: bool = False) -> dto.AgentGraph | None:
    """``None`` when the Run has neither graph nodes nor LLM/tool calls."""
    ordered = sorted(spans, key=_key)
    by_id = {sp.span_id: sp for sp in ordered}
    graph_spans = [sp for sp in ordered if sp.graph_node is not None]
    if not graph_spans:
        return _fallback(ordered, full)
    is_node = {sp.span_id for sp in graph_spans}
    execs: dict[bytes, _Exec] = {}
    for sp in graph_spans:
        gn = sp.graph_node
        assert gn is not None  # noqa: S101 - filtered above
        execs[sp.span_id] = _Exec(
            span=sp,
            label=gn.node_name,
            kind=graph_node_kind(gn.node_kind, sp.category),
            parent=_nearest(sp.span_id, by_id, is_node),
        )
    for ex in execs.values():  # node paths from the root, for collapsing
        path, parent, guard = [ex.label], ex.parent, 0
        while parent is not None and parent in execs and guard < len(execs):
            path.append(execs[parent].label)
            parent, guard = execs[parent].parent, guard + 1
        ex.path = tuple(reversed(path))

    interrupted: set[bytes] = set()
    for sp in ordered:
        if sp.approval is not None:
            owner = _self_or_nearest(sp.span_id, by_id, is_node)
            if owner is not None:
                interrupted.add(owner)

    node_of = _expanded(execs, graph_spans)
    collapsed = False
    if not full and len(set(node_of.values())) > MAX_NODES:
        node_of = {sid: "/".join(ex.path) for sid, ex in execs.items()}
        collapsed = True
    return _assemble(execs, graph_spans, node_of, interrupted, collapsed)


def _expanded(execs: dict[bytes, _Exec], graph_spans: list[r.TraceSpan]) -> dict[bytes, str]:
    """One node per execution, except that adjacent retries of a node merge (REQ-153.b)."""
    node_of: dict[bytes, str] = {}
    last_child: dict[bytes | None, bytes] = {}  # parent → last sibling execution
    for sp in graph_spans:
        ex = execs[sp.span_id]
        prev = last_child.get(ex.parent)
        if prev is not None and execs[prev].label == ex.label and execs[prev].kind == ex.kind:
            node_of[sp.span_id] = node_of[prev]
        else:
            node_of[sp.span_id] = sp.span_id.hex()
        last_child[ex.parent] = sp.span_id
    return node_of


def _assemble(
    execs: dict[bytes, _Exec],
    graph_spans: list[r.TraceSpan],
    node_of: dict[bytes, str],
    interrupted: set[bytes],
    collapsed: bool,
) -> dto.AgentGraph:
    nodes: dict[str, _Node] = {}
    for sp in graph_spans:
        ex = execs[sp.span_id]
        key = node_of[sp.span_id]
        node = nodes.get(key)
        if node is None:
            node = nodes[key] = _Node(id=f"n{len(nodes)}", label=ex.label, kind=ex.kind)
        node.spans.append(sp)
        gn = sp.graph_node
        node.error = node.error or sp.status_code == 2 or (gn is not None and gn.status == "error")
        node.interrupted = node.interrupted or sp.span_id in interrupted
    edges: dict[tuple[str, str, str], int] = {}

    def link(a: bytes, b: bytes, kind: str) -> None:
        src, dst = nodes[node_of[a]].id, nodes[node_of[b]].id
        if src != dst:
            edges[(src, dst, kind)] = edges.get((src, dst, kind), 0) + 1

    last_child: dict[bytes | None, bytes] = {}
    for sp in graph_spans:
        ex = execs[sp.span_id]
        if ex.parent is not None and ex.parent in execs:
            link(ex.parent, sp.span_id, "parent")
        prev = last_child.get(ex.parent)
        if prev is not None:
            link(prev, sp.span_id, "sequence")
        last_child[ex.parent] = sp.span_id
    return dto.AgentGraph(
        nodes=[_node_dto(n) for n in nodes.values()],
        edges=[
            dto.GraphEdge(source=s, target=t, kind=k, count=c)  # type: ignore[arg-type]
            for (s, t, k), c in edges.items()
        ],
        collapsed=collapsed,
        fallback_sequence=False,
        total_executions=len(graph_spans),
    )


def _node_dto(n: _Node) -> dto.GraphNode:
    return dto.GraphNode(
        id=n.id,
        label=n.label,
        kind=n.kind,
        executions=len(n.spans),
        status="error" if n.error else "ok",
        interrupted=n.interrupted,
        span_ids=[span_id_to_hex(sp.span_id) for sp in n.spans],
    )


def _fallback(ordered: list[r.TraceSpan], full: bool) -> dto.AgentGraph | None:
    """REQ-153.e: LLM and tool calls in start order, one node per call."""
    calls: list[tuple[r.TraceSpan, str, GraphKind]] = []
    for sp in ordered:
        if sp.llm_call is not None:
            calls.append((sp, sp.llm_call.model, "llm"))
        elif sp.tool_call is not None:
            calls.append((sp, sp.tool_call.tool_name, "tool"))
    if not calls:
        return None
    collapsed = not full and len(calls) > MAX_NODES
    nodes: dict[str, _Node] = {}
    order: list[str] = []
    for i, (sp, label, kind) in enumerate(calls):
        key = f"{kind}:{label}" if collapsed else str(i)
        node = nodes.get(key)
        if node is None:
            node = nodes[key] = _Node(id=f"n{len(nodes)}", label=label, kind=kind)
        node.spans.append(sp)
        node.error = node.error or sp.status_code == 2
        node.interrupted = node.interrupted or sp.approval is not None
        order.append(node.id)
    edges: dict[tuple[str, str], int] = {}
    for a, b in itertools.pairwise(order):
        if a != b:
            edges[(a, b)] = edges.get((a, b), 0) + 1
    return dto.AgentGraph(
        nodes=[_node_dto(n) for n in nodes.values()],
        edges=[
            dto.GraphEdge(source=a, target=b, kind="sequence", count=c)
            for (a, b), c in edges.items()
        ],
        collapsed=collapsed,
        fallback_sequence=True,
        total_executions=len(calls),
    )
