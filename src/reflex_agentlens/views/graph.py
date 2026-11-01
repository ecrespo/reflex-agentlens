"""Agent graph as Mermaid text, and replay stepping (plan tasks P6-07.1, P6-07.2; REQ-153, 154,
DD-011, Art. 7).

:func:`graph_text` renders ``AgentGraph`` as ``flowchart LR``. Node IDs are ``n0``, ``n1``, …
in the graph's order (stable for one Run). Labels are hostile input: every character outside a
small safe set becomes a Mermaid entity code (``#34;``), so a label can never close its node,
start an edge, open a comment (``%%``) or a directive, or carry markup. The renderer also runs
with ``security_level="strict"``.

Replay (REQ-154) steps the loaded spans in start order; :func:`replay_delay_ms` turns the real
gap between two spans into a delay for speeds 1, 2 and 5 (``step`` waits for the viewer).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from reflex_agentlens.query.dto import AgentGraph, GraphEdge, GraphNode, SpanNode

__all__ = [
    "MAX_LABEL",
    "GraphText",
    "escape_label",
    "graph_text",
    "node_of_span",
    "replay_delay_ms",
    "replay_order",
    "replay_step",
]

MAX_LABEL = 60
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.,:/+=?@")
CLASS_DEFS = (
    "classDef taken stroke-width:2px",
    "classDef error stroke:#B42318,stroke-width:2px",
    "classDef approval stroke-dasharray:4 2",
    "classDef retry stroke-width:3px",
    "classDef current fill:#FFF4CC",
)
_SHAPES = {
    "agent": ('(["', '"])'),
    "llm": ('["', '"]'),
    "tool": ('[/"', '"/]'),
    "approval": ('{"', '"}'),
    "node": ('("', '")'),
}


def escape_label(text: str) -> str:
    """Mermaid-safe label text: a closed character set, the rest as ``#<code>;``."""
    if len(text) > MAX_LABEL:
        text = text[: MAX_LABEL - 1] + "…"
    return "".join(c if c in _SAFE else f"#{ord(c)};" for c in text)


@dataclass(frozen=True, slots=True)
class GraphText:
    text: str
    note: str  # shown above the diagram ("" when nothing was collapsed)
    node_spans: dict[str, list[str]] = field(default_factory=dict)  # n3 -> span IDs


def _edge(edge: GraphEdge, ids: dict[str, str]) -> str | None:
    src, dst = ids.get(edge.source), ids.get(edge.target)
    if src is None or dst is None:
        return None
    arrow = "-.->" if not edge.taken else ("==>" if edge.kind == "sequence" else "-->")
    label = f"|{edge.count}x|" if edge.count > 1 else ""
    return f"  {src} {arrow}{label} {dst}"


def _classes(node: GraphNode, current: bool) -> list[str]:
    out = ["taken"] if node.executions > 0 else []
    if node.status == "error" or node.interrupted:
        out.append("error")
    if node.kind == "approval":
        out.append("approval")
    if node.executions > 1:
        out.append("retry")
    if current:
        out.append("current")
    return out


def graph_text(graph: AgentGraph, current_span: str | None = None) -> GraphText:
    ids = {node.id: f"n{i}" for i, node in enumerate(graph.nodes)}
    lines = ["flowchart LR", *(f"  {d}" for d in CLASS_DEFS)]
    spans: dict[str, list[str]] = {}
    for node in graph.nodes:
        nid = ids[node.id]
        spans[nid] = list(node.span_ids)
        label = escape_label(node.label or node.kind)
        if node.executions > 1:
            label += f" #215;{node.executions}"
        left, right = _SHAPES[node.kind]
        lines.append(f"  {nid}{left}{label}{right}")
        classes = _classes(node, current_span is not None and current_span in node.span_ids)
        if classes:
            lines.append(f"  class {nid} {','.join(classes)}")
    lines += [line for e in graph.edges if (line := _edge(e, ids)) is not None]
    notes = []
    if graph.collapsed:
        notes.append(
            f"Large graph: repeated steps are collapsed ({graph.total_executions} executions)."
        )
    if graph.fallback_sequence:
        notes.append("No agent structure was recorded; showing the call sequence.")
    return GraphText(text="\n".join(lines), note=" ".join(notes), node_spans=spans)


def node_of_span(text: GraphText, span_id: str) -> str | None:
    return next((nid for nid, spans in text.node_spans.items() if span_id in spans), None)


# ── replay (REQ-154) ─────────────────────────────────────────────────────────────────────────

SPEED_FACTORS = {"1": 1.0, "2": 2.0, "5": 5.0}
MIN_DELAY_MS, MAX_DELAY_MS = 150, 3000


def replay_order(spans: Sequence[SpanNode]) -> list[str]:
    """Real spans in start order (ties by span ID: deterministic)."""
    real = [s for s in spans if not s.placeholder]
    return [s.span_id for s in sorted(real, key=lambda s: (s.start_ns, s.span_id))]


def replay_step(order: Sequence[str], current: str | None, delta: int) -> str | None:
    """The span ``delta`` steps from ``current`` (clamped); the first one when not started."""
    if not order:
        return None
    if current not in order:
        return order[0] if delta >= 0 else order[-1]
    i = order.index(current) + delta
    return order[max(0, min(i, len(order) - 1))]


def replay_delay_ms(speed: str, gap_ns: int) -> int | None:
    """Wait before the next step; ``None`` for ``step`` (manual)."""
    factor = SPEED_FACTORS.get(speed)
    if factor is None:
        return None
    ms = max(gap_ns, 0) / 1_000_000 / factor
    return int(min(max(ms, MIN_DELAY_MS), MAX_DELAY_MS))
