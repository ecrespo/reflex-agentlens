"""GraphNode, Session key, search document and the pure pipeline (plan task P3-03.3).

REQ-033.d, REQ-036.a/c, REQ-073.a, DD-006, DD-011, Data Model §2.8 (Analyze A-08), Flow 1 step 9.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from reflex_agentlens.core.search import MAX_SEARCH_DOC_BYTES
from reflex_agentlens.ingest.cost import PricingSnapshot
from reflex_agentlens.ingest.normalize import BatchContext, normalize_batch
from reflex_agentlens.ingest.normalize.classify import classify_draft
from reflex_agentlens.ingest.normalize.graph import graph_node
from reflex_agentlens.ingest.normalize.search import search_document
from reflex_agentlens.ingest.normalize.session import external_id, session_key
from reflex_agentlens.ingest.normalize.spans import (
    FLAG_CONTENT_DROPPED,
    FLAG_REDACTED,
    FLAG_TRUNCATED,
)
from reflex_agentlens.ingest.redaction.rules import compile_rules
from reflex_agentlens.store.records import (
    PricingEntry,
    PricingVersion,
    PricingVersionMeta,
    RedactionRuleSet,
    SessionKey,
)
from tests.unit.ingest.drafts import event, mk
from tests.unit.ingest.otlp import NOW_NS, S, request, span

SECRET = b"k" * 32
AWS = "AKIA" + "IOSFODNN7EXAMPLE"

# ── graph nodes (DD-011) ──────────────────────────────────────────────────────────────────────


def node(attrs: dict[str, Any], **fields: Any) -> Any:
    d = mk(attrs, **fields)
    return graph_node(d, classify_draft(d))


def test_spec_agent_and_workflow_nodes() -> None:
    agent = node({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "planner"})
    assert (agent.node_kind, agent.node_key, agent.node_name, agent.agent_name) == (
        "agent", "planner", "planner", "planner"
    )  # fmt: skip
    flow = node({"gen_ai.operation.name": "invoke_workflow", "gen_ai.workflow.name": "triage"})
    assert (flow.node_kind, flow.node_name) == ("workflow", "triage")
    nameless = node({"gen_ai.operation.name": "invoke_agent"}, name="invoke_agent")
    assert nameless.node_name == "invoke_agent"


def test_openinference_graph_node() -> None:
    row = node({"openinference.span.kind": "AGENT", "graph.node.id": "n2",
                "graph.node.parent_id": "n1", "graph.node.name": "Researcher"})  # fmt: skip
    assert (row.node_key, row.node_name, row.explicit_parent_key) == ("n2", "Researcher", "n1")
    assert row.node_kind == "agent"
    chain = node({"openinference.span.kind": "CHAIN", "graph.node.id": 7})
    assert (chain.node_kind, chain.node_key, chain.node_name) == ("node", "7", "7")


def test_langgraph_node_from_metadata() -> None:
    meta = json.dumps({"langgraph_node": "tools", "langgraph_step": 3})
    row = node({"openinference.span.kind": "CHAIN", "metadata": meta}, status_code=2)
    assert (row.node_key, row.node_name, row.step, row.status) == ("tools", "tools", 3, "error")
    assert row.node_kind == "node"
    huge = json.dumps({"langgraph_node": "x", "langgraph_step": 2**40})
    assert node({"openinference.span.kind": "CHAIN", "metadata": huge}).step is None


def test_plain_spans_are_not_graph_nodes() -> None:
    assert node({"gen_ai.operation.name": "chat"}) is None
    assert node({"http.method": "GET"}) is None


# ── Session key (Data Model §2.8, A-08) ───────────────────────────────────────────────────────


def tag(raw: str) -> str:
    return hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:16]


def test_session_id_kept_when_not_redacted() -> None:
    attrs = {"gen_ai.conversation.id": "conv-1"}
    assert session_key(attrs, attrs, SECRET) == SessionKey("conv-1")
    assert session_key({}, {}, SECRET) is None
    oi = {"session.id": 42}
    assert session_key(oi, oi, SECRET) == SessionKey("42")


def test_redacted_ids_stay_distinct_and_raw_is_never_stored() -> None:
    """Two emails redact to the same text, yet must stay two Sessions (A-08)."""
    redacted = {"gen_ai.conversation.id": "[REDACTED:email]"}
    a = session_key({"gen_ai.conversation.id": "ana@x.io"}, redacted, SECRET)
    b = session_key({"gen_ai.conversation.id": "bob@x.io"}, redacted, SECRET)
    assert a == SessionKey(f"[REDACTED:email]~{tag('ana@x.io')}")
    assert b is not None
    assert a != b
    assert "ana" not in a.external_id
    again = session_key({"gen_ai.conversation.id": "ana@x.io"}, redacted, SECRET)
    assert again == a


def test_long_ids_are_capped_and_hashed() -> None:
    long_id = "c" * 600
    key = session_key({"session.id": long_id}, {"session.id": long_id}, SECRET)
    assert key is not None
    digest = hashlib.blake2b(long_id.encode(), digest_size=8).hexdigest()
    assert key.external_id == "c" * 480 + "…" + digest
    assert len(key.external_id) <= 512


@pytest.mark.parametrize(("value", "out"), [("x", "x"), (5, "5"), ("  ", None), (True, None),
                                            (None, None), (1.5, None)])  # fmt: skip
def test_external_id_forms(value: object, out: str | None) -> None:
    assert external_id(value) == out


# ── search document (DD-006) ──────────────────────────────────────────────────────────────────


def doc(attrs: dict[str, Any], **fields: Any) -> str | None:
    d = mk(attrs, **fields)
    return search_document(d, classify_draft(d))


def test_genai_document_has_messages_tools_and_errors() -> None:
    messages = [{"role": "user", "parts": [{"type": "text", "content": "Find the Café hours"}]},
                {"role": "assistant", "parts": [{"type": "tool_call", "name": "search_web",
                                                 "arguments": {"q": "cafe"}}]}]  # fmt: skip
    out = doc(
        {"gen_ai.operation.name": "chat", "gen_ai.input.messages": json.dumps(messages)},
        name="chat gpt-4o-mini",
        status_message="Upstream_Timeout",
        events=[
            event("exception", **{"exception.type": "db.QueryError", "exception.message": "boom"})
        ],
    )
    assert out == ("chat gpt 4o mini find the cafe hours search web q cafe upstream timeout "
                   "db queryerror boom")  # fmt: skip


def test_tool_document_has_arguments_and_result() -> None:
    out = doc({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "RefundTool",
               "gen_ai.tool.call.arguments": '{"order": 17}', "gen_ai.tool.call.result": "ok"},
              name="execute_tool")  # fmt: skip
    assert out == "execute tool refundtool order 17 ok"


def test_non_genai_document_is_name_and_status_only() -> None:
    out = doc({"http.url": "https://secret.example/q"}, name="GET /x", status_message="bad")
    assert out == "get x bad"


def test_document_is_capped_and_empty_is_none() -> None:
    big = doc({"gen_ai.operation.name": "chat", "gen_ai.prompt": "word " * 20_000})
    assert big is not None
    assert len(big.encode()) <= MAX_SEARCH_DOC_BYTES
    assert doc({}, name="") is None


# ── pipeline (Flow 1 step 9) ──────────────────────────────────────────────────────────────────

PRICING = PricingSnapshot(
    [PricingVersion(PricingVersionMeta(1, 0, "bundled", None, 0, None),
                    (PricingEntry("gpt-4o", "exact", 2_500_000_000, 10_000_000_000),))]
)  # fmt: skip


def ctx(**kw: Any) -> BatchContext:
    rules = compile_rules(RedactionRuleSet({"secrets": True, "email": True}, [], []))
    base: dict[str, Any] = {"project_id": "p1", "received_at_ns": NOW_NS, "rules": rules,
                            "session_secret": SECRET, "pricing": PRICING}  # fmt: skip
    return BatchContext(**(base | kw))


LLM: dict[str, Any] = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o",
       "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 100,
       "gen_ai.conversation.id": "ana@x.io",
       "gen_ai.input.messages": json.dumps([{"role": "user", "parts": [
           {"type": "text", "content": f"my key {AWS}"}]}])}  # fmt: skip


def test_pipeline_end_to_end() -> None:
    req = request(span(1, 1, name="chat gpt-4o", **LLM),
                  span(1, 2, parent=1, gen_ai__operation__name="execute_tool",
                       gen_ai__tool__name="search"),
                  span(1, 3, start_ago_s=0, end_ago_s=-1))  # fmt: skip
    req.resource_spans[0].scope_spans[0].spans[2].start_time_unix_nano = 0  # invalid
    result = normalize_batch(req, ctx())
    assert result.rejections.counts() == {"missing_start_time": 1}
    llm_span, tool_span = result.batch.spans
    assert result.batch.project_id == "p1"
    assert (llm_span.category, tool_span.category) == ("llm", "tool")
    assert llm_span.llm_call is not None
    assert llm_span.llm_call.cost_nusd == 3_500_000  # 1000 x $2.5/M + 100 x $10/M
    assert llm_span.llm_call.pricing_version == 1
    assert AWS not in json.dumps(llm_span.attributes)
    assert llm_span.flags & FLAG_REDACTED
    assert llm_span.session == SessionKey(f"[REDACTED:email]~{tag('ana@x.io')}")
    assert llm_span.search_doc is not None
    assert "akia" not in llm_span.search_doc
    assert "redacted secrets" in llm_span.search_doc
    assert tool_span.tool_call is not None
    assert tool_span.tool_call.tool_name == "search"
    assert tool_span.parent_span_id == (1).to_bytes(8, "big")
    assert llm_span.source.service_name == "svc"
    assert llm_span.mapping_version == 1
    assert llm_span.scope_name == "test.scope"


def test_pipeline_is_deterministic() -> None:
    req = request(
        span(1, 1, **LLM),
        span(
            2,
            1,
            gen_ai__operation__name="invoke_agent",
            gen_ai__agent__name="a",
            gen_ai__usage__input_tokens=5,
        ),
    )
    first, second = normalize_batch(req, ctx()), normalize_batch(req, ctx())
    assert first.batch == second.batch
    agent = first.batch.spans[1]
    assert (agent.agent_usage_input, agent.graph_node is not None) == (5, True)


def test_hook_failure_rejects_only_that_span() -> None:
    def hook(view: dict[str, Any]) -> dict[str, Any]:
        if view["name"] == "bad":
            raise ValueError("no")
        return view

    req = request(span(1, 1, name="good"), span(1, 2, name="bad"), span(1, 3, name="also good"))
    result = normalize_batch(req, ctx(hook=hook))
    assert [s.name for s in result.batch.spans] == ["good", "also good"]
    assert result.rejections.counts() == {"redaction_hook_error": 1}


def test_content_off_and_truncation_run_after_redaction() -> None:
    req = request(span(1, 1, **LLM, note="x" * 200))
    (out,) = normalize_batch(
        req, ctx(content_storage=False, max_attribute_value_bytes=64)
    ).batch.spans
    assert "gen_ai.input.messages" not in out.attributes
    assert out.flags & FLAG_CONTENT_DROPPED
    assert out.flags & FLAG_TRUNCATED
    assert out.llm_call is not None
    assert out.llm_call.content_dropped
    assert out.llm_call.input_tokens == 1000  # metadata kept


def test_clock_skew_and_retention_limits() -> None:
    future = span(1, 1, start_ago_s=-2 * 3600, end_ago_s=-2 * 3600 - 1)
    old = span(1, 2, start_ago_s=3 * 86400, end_ago_s=3 * 86400 - 1)
    result = normalize_batch(request(future, old), ctx(retention_max_age_s=86400))
    assert result.rejections.counts() == {"future_timestamp": 1, "older_than_retention": 1}
    assert result.batch.spans == ()


def test_long_names_and_scope_fields_are_cut() -> None:
    req = request(span(1, 1, name="n" * 2000))
    req.resource_spans[0].scope_spans[0].scope.version = "v" * 100
    (out,) = normalize_batch(req, ctx()).batch.spans
    assert (len(out.name), len(out.scope_version or "")) == (1024, 64)
    assert out.end_ns - out.start_ns == 5 * S
