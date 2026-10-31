"""Semconv constants, mapping table and dialects (plan tasks P3-01.1, P3-01.2).

REQ-035.a (one declarative, versioned table covering spec, deprecated, OpenInference and
PydanticAI names), REQ-035.b (spec attribute wins), REQ-251 (sibling vocabulary).
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import pytest

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.core.semconv.dialects import conflicts, detect, source_keys, table_markdown
from reflex_agentlens.core.semconv.mapping import (
    FIELDS,
    MAPPING_VERSION,
    Attr,
    Json,
    Mapped,
    Prefixed,
    classify,
    flat,
    params,
    resolve,
    resolve_with_source,
)


def test_pins_are_recorded() -> None:
    """Q-T2: the GenAI conventions are pinned to a full commit SHA on core 1.44.0."""
    assert re.fullmatch(r"[0-9a-f]{40}", k.SEMCONV_GENAI_COMMIT)
    assert k.SEMCONV_CORE == "1.44.0"
    assert MAPPING_VERSION == 1


def test_every_mapped_key_is_a_declared_constant() -> None:
    """REQ-035.a: the table only names keys declared in ``constants``."""
    for name, sources in FIELDS.items():
        for source in sources:
            keys = source.keys if isinstance(source, Prefixed) else (source.key,)
            for key in keys:
                assert key in k.ALL_KEYS or key.startswith(k.APPROVAL_PREFIXES), (name, key)
    assert "gen_ai.operation.name" in k.ALL_KEYS
    assert "gen_ai.client.token.usage" not in k.ALL_KEYS  # metric, not an attribute


# one row per (field, source): (field, attributes, expected value)
ROWS: list[tuple[str, dict[str, Any], Any]] = [
    ("operation", {"gen_ai.operation.name": "chat"}, "chat"),
    ("operation", {"openinference.span.kind": "LLM"}, "chat"),
    ("operation", {"openinference.span.kind": "EMBEDDING"}, "embeddings"),
    ("operation", {"openinference.span.kind": "TOOL"}, "execute_tool"),
    ("operation", {"langsmith.span.kind": "llm"}, "chat"),
    ("operation", {"openinference.span.kind": "CHAIN"}, None),
    ("provider", {"gen_ai.provider.name": "openai"}, "openai"),
    ("provider", {"gen_ai.system": "anthropic"}, "anthropic"),
    ("provider", {"llm.provider": "azure"}, "azure"),
    ("provider", {"llm.system": "openai"}, "openai"),
    ("request_model", {"gen_ai.request.model": "gpt-4o"}, "gpt-4o"),
    ("request_model", {"llm.model_name": "gpt-4o"}, "gpt-4o"),
    ("request_model", {"langsmith.metadata.ls_model_name": "m"}, "m"),
    ("response_model", {"gen_ai.response.model": "gpt-4o-2024"}, "gpt-4o-2024"),
    ("response_model", {"llm.model_name": "gpt-4o"}, "gpt-4o"),
    ("input_tokens", {"gen_ai.usage.input_tokens": 10}, 10),
    ("input_tokens", {"gen_ai.usage.prompt_tokens": 11}, 11),
    ("input_tokens", {"llm.token_count.prompt": 12}, 12),
    ("output_tokens", {"gen_ai.usage.output_tokens": 5}, 5),
    ("output_tokens", {"gen_ai.usage.completion_tokens": 6}, 6),
    ("output_tokens", {"llm.token_count.completion": 7}, 7),
    ("cached_input_tokens", {"gen_ai.usage.cache_read.input_tokens": 3}, 3),
    ("cached_input_tokens", {"llm.token_count.prompt_details.cache_read": 4}, 4),
    ("cache_write_tokens", {"gen_ai.usage.cache_write.input_tokens": 1}, 1),
    ("cache_write_tokens", {"gen_ai.usage.cache_creation.input_tokens": 2}, 2),
    ("cache_write_tokens", {"llm.token_count.prompt_details.cache_write": 3}, 3),
    ("reasoning_tokens", {"gen_ai.usage.reasoning.output_tokens": 9}, 9),
    ("finish_reasons", {"gen_ai.response.finish_reasons": ["stop"]}, ["stop"]),
    ("tool_name", {"gen_ai.tool.name": "search"}, "search"),
    ("tool_name", {"tool.name": "lookup"}, "lookup"),
    ("tool_call_id", {"gen_ai.tool.call.id": "c1"}, "c1"),
    ("tool_call_id", {"tool_call.id": "c2"}, "c2"),
    ("tool_type", {"gen_ai.tool.type": "function"}, "function"),
    ("tool_args", {"gen_ai.tool.call.arguments": '{"q":1}'}, '{"q":1}'),
    ("tool_args", {"input.value": "x", "openinference.span.kind": "TOOL"}, "x"),
    ("tool_args", {"input.value": "x", "openinference.span.kind": "LLM"}, None),
    ("tool_result", {"gen_ai.tool.call.result": "ok"}, "ok"),
    ("tool_result", {"output.value": "y", "openinference.span.kind": "TOOL"}, "y"),
    ("agent_name", {"gen_ai.agent.name": "triage"}, "triage"),
    ("agent_name", {"agent_name": "support"}, "support"),
    ("agent_name", {"agent.name": "planner"}, "planner"),
    ("agent_name", {"graph.node.name": "writer"}, "writer"),
    ("workflow_name", {"gen_ai.workflow.name": "wf"}, "wf"),
    ("session_id", {"gen_ai.conversation.id": "c-1"}, "c-1"),
    ("session_id", {"session.id": "s-1"}, "s-1"),
    ("session_id", {"langsmith.metadata.thread_id": "t-1"}, "t-1"),
    ("session_id", {"langsmith.metadata.session_id": "ls-1"}, "ls-1"),
    ("session_id", {"langsmith.metadata.conversation_id": "lc-1"}, "lc-1"),
    ("graph_node_id", {"graph.node.id": "n1"}, "n1"),
    ("graph_node_id", {"metadata": json.dumps({"langgraph_node": "agent"})}, "agent"),
    ("graph_node_id", {"metadata": "not json"}, None),
    ("graph_node_parent_id", {"graph.node.parent_id": "n0"}, "n0"),
    ("graph_step", {"metadata": json.dumps({"langgraph_step": 3})}, 3),
    ("error_type", {"error.type": "TimeoutError"}, "TimeoutError"),
    ("approval_id", {"reflex_agentkit.approval.id": "a1"}, "a1"),
    ("approval_id", {"reflex_mcphost.approval.id": "m1"}, "m1"),
    ("approval_decision", {"reflex_agentkit.approval.decision": "approved"}, "approved"),
    ("approval_decision", {"reflex_mcphost.approval.decision": "rejected"}, "rejected"),
    ("approval_approver", {"reflex_agentkit.approval.approver": "bo"}, "bo"),
    ("approval_approver", {"reflex_mcphost.approval.approver": "ana"}, "ana"),
    ("approval_tool_call_id", {"reflex_agentkit.approval.tool_call.id": "c9"}, "c9"),
    ("approval_tool_call_id", {"reflex_mcphost.approval.tool_call.id": "c8"}, "c8"),
    ("deferral_name", {"pydantic_ai.tool.deferral.name": "refund"}, "refund"),
]


@pytest.mark.parametrize(("field", "attrs", "want"), ROWS)
def test_mapping_rows(field: str, attrs: dict[str, Any], want: Any) -> None:
    """REQ-035.a: each source of each field, in isolation."""
    assert resolve(attrs, field) == want


def test_every_source_has_a_row() -> None:
    covered = {(f, key) for f, attrs, want in ROWS if want is not None for key in attrs}
    for name in FIELDS:
        for key in source_keys(name):
            assert (name, key) in covered, (name, key)


def test_spec_attribute_wins() -> None:
    """REQ-035.b: spec and mapped alternative with different values → the spec value."""
    attrs = {
        "gen_ai.request.model": "gpt-4o",
        "llm.model_name": "gpt-4o-mini",
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.prompt_tokens": 99,
        "llm.token_count.prompt": 77,
        "gen_ai.provider.name": "openai",
        "gen_ai.system": "azure",
    }
    assert resolve(attrs, "request_model") == "gpt-4o"
    assert resolve(attrs, "input_tokens") == 10
    assert resolve(attrs, "provider") == "openai"
    found = resolve_with_source(attrs, "input_tokens")
    assert found is not None
    assert isinstance(found.source, Attr)
    assert found.source.dialect == "spec"
    assert conflicts(attrs) == {
        "provider": ("openai", "azure"),
        "request_model": ("gpt-4o", "gpt-4o-mini"),
        "input_tokens": (10, 99, 77),
    }
    assert detect(attrs) == {"spec", "deprecated", "openinference"}


def test_empty_values_count_as_absent() -> None:
    assert resolve({"gen_ai.request.model": "", "llm.model_name": "m"}, "request_model") == "m"


def test_params() -> None:
    spec = {"gen_ai.request.temperature": 0.2, "gen_ai.request.max_tokens": 100}
    assert params(spec) == {"temperature": 0.2, "max_tokens": 100}
    oi = {"llm.invocation_parameters": json.dumps({"temperature": 0.7})}
    assert params(oi) == {"temperature": 0.7}
    pai = {"model_request_parameters": {"tools": []}, "llm.invocation_parameters": "{}"}
    assert params(pai) == {"tools": []}
    assert params({"llm.invocation_parameters": "[1, 2]"}) == {}


def test_flat_reassembles_openinference_lists() -> None:
    attrs = {
        "llm.input_messages.1.message.role": "user",
        "llm.input_messages.0.message.role": "system",
        "llm.input_messages.0.message.content": "be brief",
        "llm.input_messages.10.message.role": "assistant",
        "llm.input_messages.x.message.role": "ignored",
        "llm.output_messages.0.message.role": "assistant",
    }
    assert flat(attrs, "llm.input_messages") == [
        {"message.role": "system", "message.content": "be brief"},
        {"message.role": "user"},
        {"message.role": "assistant"},
    ]


# ── classification (Tech Design §5.3 rules 1-5) ──────────────────────────────────────────────

CLASSIFY: list[tuple[dict[str, Any], str, bool, str]] = [
    ({"gen_ai.operation.name": "chat"}, "llm", True, "1:operation"),
    ({"gen_ai.operation.name": "text_completion"}, "llm", True, "1:operation"),
    ({"gen_ai.operation.name": "generate_content"}, "llm", True, "1:operation"),
    ({"gen_ai.operation.name": "embeddings"}, "llm", True, "1:operation"),
    ({"gen_ai.operation.name": "execute_tool"}, "tool", True, "1:operation"),
    ({"gen_ai.operation.name": "invoke_agent"}, "agent", True, "1:operation"),
    ({"gen_ai.operation.name": "create_agent"}, "agent", True, "1:operation"),
    ({"gen_ai.operation.name": "invoke_workflow"}, "agent", True, "1:operation"),
    ({"gen_ai.operation.name": "retrieval"}, "generic_genai", True, "1:operation"),
    ({"gen_ai.operation.name": "plan"}, "generic_genai", True, "1:operation"),
    ({"gen_ai.operation.name": "update_memory"}, "generic_genai", True, "1:operation"),
    ({"openinference.span.kind": "LLM"}, "llm", True, "2:openinference"),
    ({"openinference.span.kind": "EMBEDDING"}, "llm", True, "2:openinference"),
    ({"openinference.span.kind": "TOOL"}, "tool", True, "2:openinference"),
    ({"openinference.span.kind": "AGENT"}, "agent", True, "2:openinference"),
    (
        {"openinference.span.kind": "CHAIN", "metadata": '{"langgraph_node": "agent"}'},
        "agent",
        True,
        "2:langgraph-node",
    ),
    ({"openinference.span.kind": "CHAIN"}, "generic_genai", True, "2:openinference"),
    ({"openinference.span.kind": "RETRIEVER"}, "generic_genai", True, "2:openinference"),
    ({"langsmith.span.kind": "llm"}, "llm", True, "3:ls"),
    ({"langsmith.span.kind": "tool"}, "tool", True, "3:ls"),
    ({"langsmith.span.kind": "chain"}, "generic_genai", True, "3:langsmith"),
    ({"reflex_agentkit.approval.id": "a1"}, "approval", True, "4:approval"),
    ({"pydantic_ai.tool.deferral.name": "refund"}, "approval", True, "4:approval"),
    (
        {"gen_ai.operation.name": "custom_op", "reflex_mcphost.approval.id": "m"},
        "approval",
        True,
        "4:approval",
    ),
    ({"gen_ai.operation.name": "custom_op"}, "generic_genai", True, "5:fallback"),
    ({"gen_ai.agent.name": "x"}, "generic_genai", True, "5:fallback"),
    ({"agent_name": "pai"}, "generic_genai", True, "5:fallback"),
    ({"http.method": "GET"}, "generic", False, "5:fallback"),
    ({}, "generic", False, "5:fallback"),
]


@pytest.mark.parametrize(("attrs", "category", "genai", "rule"), CLASSIFY)
def test_classification_rules(attrs: dict[str, Any], category: str, genai: bool, rule: str) -> None:
    got = classify(attrs)
    assert (got.category, got.is_genai, got.rule) == (category, genai, rule)


def test_spec_operation_beats_other_dialects() -> None:
    got = classify({"gen_ai.operation.name": "execute_tool", "openinference.span.kind": "LLM"})
    assert (got.category, got.operation) == ("tool", "execute_tool")


def test_table_renders_to_markdown() -> None:
    """The documentation table is generated from code (input for P9-01)."""
    md = table_markdown()
    assert md.startswith("| Field | Sources (priority) |\n|---|---|\n")
    assert md.count("\n") == len(FIELDS) + 2
    assert "| input_tokens | `gen_ai.usage.input_tokens` · `gen_ai.usage.prompt_tokens`" in md
    assert "`json(metadata, $.langgraph_node)`" in md
    assert "`openinference.span.kind` (mapped)" in md
    assert "(when `openinference.span.kind=TOOL`)" in md
    assert "`reflex_agentkit.approval.id` / `reflex_mcphost.approval.id`" in md


def test_source_types_are_frozen() -> None:
    for source in (Attr("a"), Json("a", (), "spec"), Mapped("a", {}, "spec")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            source.key = "b"  # type: ignore[misc]
    assert Prefixed(("a.", "b."), "id").keys == ("a.id", "b.id")


def test_detect_dialects_without_prefix() -> None:
    attrs = {"agent_name": "a", "session.id": "s", "reflex_agentkit.approval.id": "x", "k": 1}
    assert detect(attrs) == {"pydantic_ai", "openinference", "reflex"}
