"""Approval derivation (plan task P3-03.2; REQ-033.c, DD-010, Tech Design §5.4).

Sources, first match wins:

1. ``reflex_agentkit.approval.*`` / ``reflex_mcphost.approval.*``: the span is the approval
   (start = requested, end = decided). An unknown or missing decision is ``pending``.
2. PydanticAI deferred tools (``pydantic_ai.tool.deferral.name``) → kind ``deferral``,
   decision ``pending``, linked to the tool call by ``gen_ai.tool.call.id``.

Derived independently of the span category: a deferral sits on an ``execute_tool`` span, which
also gets its ToolCall. MCP elicitation (COULD) is not derived yet: no attribute carries the
user's answer, so its decision can't be told apart from a timeout.
"""

from __future__ import annotations

from typing import get_args

from reflex_agentlens.core.semconv.mapping import resolve
from reflex_agentlens.ingest.normalize.common import text
from reflex_agentlens.ingest.normalize.spans import SpanDraft
from reflex_agentlens.store.records import ApprovalDecision, ApprovalRow

__all__ = ["approval"]


def _decision(value: object) -> ApprovalDecision:
    if isinstance(value, str):
        lowered = value.strip().lower()
        for decision in get_args(ApprovalDecision):
            if decision == lowered:
                return decision  # type: ignore[no-any-return]
    return "pending"


def approval(d: SpanDraft) -> ApprovalRow | None:
    attrs = d.attributes
    key = resolve(attrs, "approval_id")
    if key is not None:
        decision = _decision(resolve(attrs, "approval_decision"))
        decided = None if decision == "pending" else d.end_ns
        return ApprovalRow(
            kind="approval",
            decision=decision,
            requested_ns=d.start_ns,
            approval_key=text(str(key), 256),
            approver=text(resolve(attrs, "approval_approver"), 256),
            decided_ns=decided,
            wait_ns=None if decided is None else decided - d.start_ns,
            tool_call_id=text(resolve(attrs, "approval_tool_call_id"), 256)
            or text(resolve(attrs, "tool_call_id"), 256),
            tool_name=text(resolve(attrs, "tool_name"), 256),
            node_name=text(resolve(attrs, "agent_name"), 256),
        )
    deferred = text(resolve(attrs, "deferral_name"), 256)
    if deferred is not None:
        return ApprovalRow(
            kind="deferral",
            decision="pending",
            requested_ns=d.start_ns,
            tool_call_id=text(resolve(attrs, "tool_call_id"), 256),
            tool_name=deferred,
            node_name=text(resolve(attrs, "agent_name"), 256),
        )
    return None
