"""GraphNode derivation (plan task P3-03.3; REQ-033.d, DD-011, Data Model §2.14).

Sources, first match wins:

1. Spec agent spans: ``invoke_agent`` / ``create_agent`` (node kind ``agent``, named by the agent)
   and ``invoke_workflow`` (kind ``workflow``, named by ``gen_ai.workflow.name``).
2. OpenInference ``graph.node.id`` (+ ``graph.node.parent_id``, ``graph.node.name``).
3. LangGraph: ``json(metadata, $.langgraph_node)`` with step ``$.langgraph_step``.

Edges and execution counts are assembled at read time by ``query.graph`` from these rows plus
the span parent chain, so only the node itself is derived here.
"""

from __future__ import annotations

from typing import Literal

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.core.semconv.mapping import Classification, resolve
from reflex_agentlens.ingest.normalize.common import count, outcome, text
from reflex_agentlens.ingest.normalize.spans import SpanDraft
from reflex_agentlens.store.records import GraphNodeRow

__all__ = ["graph_node"]


def _id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return text(value, 256)


def graph_node(d: SpanDraft, cls: Classification) -> GraphNodeRow | None:
    attrs = d.attributes
    agent = text(resolve(attrs, "agent_name"), 256)
    node_id = _id(resolve(attrs, "graph_node_id"))
    if node_id is None and cls.category != "agent":
        return None  # only agent spans and explicit graph nodes are nodes
    node_name = text(attrs.get(k.OI_GRAPH_NODE_NAME), 256)
    op = attrs.get(k.GEN_AI_OPERATION_NAME)
    kind: Literal["agent", "workflow", "node"]
    name: str | None
    if op == k.OP_INVOKE_WORKFLOW:
        kind, name = "workflow", text(resolve(attrs, "workflow_name"), 256) or agent
    elif op in k.AGENT_OPERATIONS or attrs.get(k.OI_SPAN_KIND) == "AGENT":
        kind, name = "agent", agent or node_name
    else:  # an OpenInference or LangGraph graph node on a chain/other span
        kind, name = "node", node_name
    name = name or node_id
    name = name or text(d.name, 256) or "unknown"
    status, _ = outcome(d)
    return GraphNodeRow(
        node_key=node_id or name,
        node_name=name,
        node_kind=kind,
        start_ns=d.start_ns,
        end_ns=d.end_ns,
        status=status,
        explicit_parent_key=_id(resolve(attrs, "graph_node_parent_id")),
        step=_step(resolve(attrs, "graph_step")),
        agent_name=agent,
    )


def _step(value: object) -> int | None:
    step = count(value)
    return step if step is not None and step <= 2**31 - 1 else None  # INT column
