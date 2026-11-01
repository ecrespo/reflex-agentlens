"""LLMCall, agent usage, ToolCall and Approval derivation (plan tasks P3-03.1, P3-03.2).

REQ-033.a/b/c, REQ-032.d, DD-010, DD-012, Tech Design §5.3/§5.4.
"""

from __future__ import annotations

import json

import pytest

from reflex_agentlens.ingest.normalize.approval import approval
from reflex_agentlens.ingest.normalize.classify import classify_draft
from reflex_agentlens.ingest.normalize.common import count, outcome, text
from reflex_agentlens.ingest.normalize.llm import agent_usage, finish_reasons, llm_call
from reflex_agentlens.ingest.normalize.spans import FLAG_CONTENT_DROPPED
from reflex_agentlens.ingest.normalize.tool import tool_call
from tests.unit.ingest.drafts import BASE, event, mk
from tests.unit.ingest.otlp import S

# ── fixtures per dialect ──────────────────────────────────────────────────────────────────────

SPEC_LLM = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.response.model": "gpt-4o-2024-08-06",
    "gen_ai.usage.input_tokens": 120,
    "gen_ai.usage.output_tokens": 30,
    "gen_ai.usage.cache_read.input_tokens": 100,
    "gen_ai.usage.cache_creation.input_tokens": 7,
    "gen_ai.usage.reasoning.output_tokens": 12,
    "gen_ai.response.finish_reasons": ["stop"],
    "gen_ai.request.temperature": 0.2,
    "gen_ai.request.max_tokens": 256,
}
OPENINFERENCE_LLM = {
    "openinference.span.kind": "LLM",
    "llm.provider": "anthropic",
    "llm.model_name": "claude-sonnet",
    "llm.token_count.prompt": 50,
    "llm.token_count.completion": 9,
    "llm.token_count.prompt_details.cache_read": 40,
    "llm.invocation_parameters": json.dumps({"temperature": 0, "max_tokens": 64}),
}
LEGACY_LLM = {
    "gen_ai.operation.name": "text_completion",
    "gen_ai.system": "cohere",
    "gen_ai.request.model": "command-r",
    "gen_ai.usage.prompt_tokens": "15",
    "gen_ai.usage.completion_tokens": 4.0,
}


def test_spec_llm_span() -> None:
    d = mk(SPEC_LLM)
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert (row.operation, row.provider) == ("chat", "openai")
    assert (row.request_model, row.response_model, row.model) == (
        "gpt-4o", "gpt-4o-2024-08-06", "gpt-4o-2024-08-06"
    )  # fmt: skip
    assert (row.input_tokens, row.output_tokens) == (120, 30)
    assert (row.cached_input_tokens, row.cache_write_tokens, row.reasoning_tokens) == (100, 7, 12)
    assert row.finish_reasons == ["stop"]
    assert row.params == {"temperature": 0.2, "max_tokens": 256}
    assert (row.start_ns, row.duration_ns) == (BASE.start_ns, 3 * S)
    assert (row.status, row.error_type, row.unpriced, row.cost_nusd) == ("ok", None, True, None)
    assert not row.content_dropped


def test_openinference_llm_span() -> None:
    d = mk(OPENINFERENCE_LLM)
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert (row.operation, row.provider, row.model) == ("chat", "anthropic", "claude-sonnet")
    assert (row.input_tokens, row.output_tokens, row.cached_input_tokens) == (50, 9, 40)
    assert row.params == {"temperature": 0, "max_tokens": 64}


def test_openinference_embedding_operation() -> None:
    d = mk({"openinference.span.kind": "EMBEDDING", "llm.model_name": "e5"})
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert row.operation == "embeddings"


def test_legacy_llm_span_coerces_counts() -> None:
    d = mk(LEGACY_LLM)
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert (row.operation, row.provider, row.model) == ("text_completion", "cohere", "command-r")
    assert (row.input_tokens, row.output_tokens) == (15, 4)


def test_model_falls_back_to_unknown() -> None:
    d = mk({"gen_ai.operation.name": "chat"})
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert row.model == "unknown"
    assert row.provider is None


def test_non_llm_spans_get_no_llm_call() -> None:
    for attrs in ({"gen_ai.operation.name": "execute_tool"}, {"http.method": "GET"}):
        d = mk(attrs)
        assert llm_call(d, classify_draft(d)) is None


def test_llm_error_from_status_and_exception_event() -> None:
    d = mk(SPEC_LLM, status_code=2, events=[event("exception", **{"exception.type": "Timeout"})])
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert (row.status, row.error_type) == ("error", "Timeout")
    d = mk(SPEC_LLM | {"error.type": "rate_limited"}, status_code=2)
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert row.error_type == "rate_limited"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["stop", "length"], ["stop", "length"]),
        ('["tool_calls"]', ["tool_calls"]),
        ("stop", ["stop"]),
        ("[not json", ["[not json"]),
        ([1, "stop", ""], ["stop"]),
        (7, []),
    ],
)
def test_finish_reasons_forms(value: object, expected: list[str]) -> None:
    assert finish_reasons(mk({"gen_ai.response.finish_reasons": value})) == expected


def test_finish_reasons_from_deprecated_choice_events() -> None:
    choice = event("gen_ai.choice", index=0, finish_reason="stop", message='{"content": "hi"}')
    d = mk({"gen_ai.operation.name": "chat"}, events=[choice])
    assert finish_reasons(d) == ["stop"]


def test_unserializable_params_are_dropped() -> None:
    d = mk({"gen_ai.operation.name": "chat", "gen_ai.request.temperature": float("nan")})
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert row.params == {}


def test_content_dropped_is_carried_from_the_flag() -> None:
    d = mk(SPEC_LLM, flags=FLAG_CONTENT_DROPPED)
    row = llm_call(d, classify_draft(d))
    assert row is not None
    assert row.content_dropped


# ── DD-012 agent usage ────────────────────────────────────────────────────────────────────────


def test_agent_usage_only_on_agent_spans() -> None:
    """PydanticAI reports usage at both levels: agent spans keep theirs, totals use LLM rows."""
    agent = mk({"gen_ai.operation.name": "invoke_agent", "gen_ai.usage.input_tokens": 300,
                "gen_ai.usage.output_tokens": 40})  # fmt: skip
    assert agent_usage(agent, classify_draft(agent)) == (300, 40)
    llm = mk(SPEC_LLM)
    assert agent_usage(llm, classify_draft(llm)) == (None, None)


def test_agent_usage_falls_back_to_aggregated_usage() -> None:
    agent = mk({"gen_ai.operation.name": "invoke_agent", "gen_ai.aggregated_usage.input_tokens": 9,
                "gen_ai.usage.total_tokens": 999})  # fmt: skip
    assert agent_usage(agent, classify_draft(agent)) == (9, None)


# ── tools ─────────────────────────────────────────────────────────────────────────────────────


def test_spec_tool_span() -> None:
    d = mk({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search",
            "gen_ai.tool.call.id": "call_1", "gen_ai.tool.type": "function"})  # fmt: skip
    row = tool_call(d, classify_draft(d))
    assert row is not None
    assert (row.tool_name, row.tool_call_id, row.tool_type, row.status) == (
        "search", "call_1", "function", "ok"
    )  # fmt: skip
    assert row.duration_ns == 3 * S


def test_openinference_tool_span() -> None:
    d = mk({"openinference.span.kind": "TOOL", "tool.name": "calc", "tool_call.id": "t9"})
    row = tool_call(d, classify_draft(d))
    assert row is not None
    assert (row.tool_name, row.tool_call_id) == ("calc", "t9")


def test_tool_errors_from_error_type_and_status() -> None:
    d = mk({"gen_ai.operation.name": "execute_tool", "error.type": "ValueError"})
    row = tool_call(d, classify_draft(d))
    assert row is not None
    assert (row.status, row.error_type) == ("error", "ValueError")
    d = mk({"gen_ai.operation.name": "execute_tool"}, status_code=2)
    row = tool_call(d, classify_draft(d))
    assert row is not None
    assert (row.status, row.error_type, row.tool_name) == ("error", None, "unknown")


def test_non_tool_span_gets_no_tool_call() -> None:
    d = mk(SPEC_LLM)
    assert tool_call(d, classify_draft(d)) is None


# ── approvals (DD-010) ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", ["reflex_agentkit.approval.", "reflex_mcphost.approval."])
@pytest.mark.parametrize("decision", ["approved", "rejected", "edited", "timeout"])
def test_approval_vocabulary(prefix: str, decision: str) -> None:
    d = mk({f"{prefix}id": "ap-1", f"{prefix}decision": decision, f"{prefix}approver": "ana",
            f"{prefix}tool_call.id": "call_7", "gen_ai.tool.name": "pay",
            "gen_ai.agent.name": "billing"})  # fmt: skip
    assert classify_draft(d).category == "approval"
    row = approval(d)
    assert row is not None
    assert (row.kind, row.decision, row.approval_key, row.approver) == (
        "approval", decision, "ap-1", "ana"
    )  # fmt: skip
    assert (row.requested_ns, row.decided_ns, row.wait_ns) == (BASE.start_ns, BASE.end_ns, 3 * S)
    assert (row.tool_call_id, row.tool_name, row.node_name) == ("call_7", "pay", "billing")


@pytest.mark.parametrize("decision", [None, "pending", "maybe", " Approved "])
def test_approval_pending_and_unknown_decisions(decision: str | None) -> None:
    attrs = {"reflex_agentkit.approval.id": "ap-2"}
    if decision is not None:
        attrs["reflex_agentkit.approval.decision"] = decision
    row = approval(mk(attrs))
    assert row is not None
    if decision == " Approved ":
        assert row.decision == "approved"
    else:
        assert (row.decision, row.decided_ns, row.wait_ns) == ("pending", None, None)


def test_approval_links_the_spans_own_tool_call_id() -> None:
    row = approval(mk({"reflex_agentkit.approval.id": "a", "gen_ai.tool.call.id": "call_3"}))
    assert row is not None
    assert row.tool_call_id == "call_3"


def test_pydantic_ai_deferral_is_a_pending_approval_on_a_tool_span() -> None:
    d = mk(
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "transfer",
            "gen_ai.tool.call.id": "call_5",
            "pydantic_ai.tool.deferral.name": "transfer",
        }
    )
    cls = classify_draft(d)
    assert cls.category == "tool"
    assert tool_call(d, cls) is not None  # the span keeps its ToolCall too
    row = approval(d)
    assert row is not None
    assert (row.kind, row.decision, row.tool_call_id, row.tool_name) == (
        "deferral", "pending", "call_5", "transfer"
    )  # fmt: skip
    assert row.approval_key is None
    assert row.decided_ns is None


def test_no_approval_without_vocabulary() -> None:
    assert approval(mk(SPEC_LLM)) is None


# ── coercions ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5), (0, 0), ("12", 12), (" 7 ", 7), (3.0, 3), (-1, None), (2.5, None), (True, None),
     ("x", None), ("\uff11\uff12", None), (float("inf"), None), (2**63, None), ("9" * 20, None),
     (None, None), ([1], None)],
)  # fmt: skip
def test_count(value: object, expected: int | None) -> None:
    assert count(value) == expected


def test_text_and_outcome() -> None:
    assert text("abcdef", 3) == "abc"
    assert text("", 3) is None
    assert text(5, 3) is None
    assert outcome(mk()) == ("ok", None)
    assert outcome(mk(status_code=2)) == ("error", None)
    long_type = mk(events=[event("exception", **{"exception.type": "E" * 300})], status_code=2)
    error_type = outcome(long_type)[1]
    assert error_type is not None
    assert len(error_type) == 128
