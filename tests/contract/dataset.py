"""Seeded contract dataset of normalized spans (plan task P2-04.1; ported from spike S2 ``gen.py``).

Runs of ~20 spans: an agent root, graph nodes, LLM calls with tokens and cost (one model
unpriced), tool calls with some errors, a few approvals; Sessions of 1-5 Runs; 5 Sources.
Same seed → same spans (``random.Random`` only, no clock or uuid sources).
"""

from __future__ import annotations

import random
from collections.abc import Iterator

from reflex_agentlens.core.money import llm_call_cost_nusd
from reflex_agentlens.store.attrs import JsonValue, attrs_hash
from reflex_agentlens.store.records import (
    ApprovalRow,
    GraphNodeRow,
    LLMCallRow,
    NormalizedSpan,
    ResourceIn,
    SessionKey,
    SourceKey,
    ToolCallRow,
)

NS = 1_000_000_000
BASE_NS = 1_790_812_800 * NS  # 2026-10-01T00:00:00Z
DAYS = 30
MAPPING_VERSION = 1
RECEIVED_NS = BASE_NS + 40 * 86_400 * NS

SOURCES = [
    SourceKey("support-agent", "shop"),
    SourceKey("refund-agent", "shop"),
    SourceKey("research-bot"),
    SourceKey("unknown_service"),
    SourceKey("eval-runner", "lab"),
]
AGENTS = ["triage", "refunds", "researcher", "planner", "writer"]
# (provider, model, input, output, cached) nano-USD per Mtok; None = unpriced
MODELS: list[tuple[str, str, int | None, int | None, int | None]] = [
    ("openai", "gpt-4o", 2_500_000_000, 10_000_000_000, 1_250_000_000),
    ("openai", "gpt-4o-mini", 150_000_000, 600_000_000, 75_000_000),
    ("anthropic", "claude-sonnet-5-5", 3_000_000_000, 15_000_000_000, 300_000_000),
    ("google", "gemini-2.5-flash", 300_000_000, 2_500_000_000, None),
    ("ollama", "llama3.3:70b", None, None, None),
]
TOOLS = ["search_web", "db.query", "RefundTool", "get_order", "send_email", "kb-lookup"]
NODES = ["router", "retrieve", "plan", "act", "reflect", "respond"]
WORDS = [
    "order",
    "refund",
    "customer",
    "weather",
    "report",
    "invoice",
    "delayed",
    "shipment",
    "password",
    "reset",
    "account",
    "billing",
    "error",
    "timeout",
    "café",
    "naïve",
    "résumé",
    "policy",
    "warranty",
    "upgrade",
    "cancel",
    "subscription",
]


def resource_for(source: SourceKey) -> ResourceIn:
    attrs: dict[str, JsonValue] = {"service.name": source.service_name}
    if source.service_namespace:
        attrs["service.namespace"] = source.service_namespace
    return ResourceIn(attrs_hash(attrs), attrs)


class Generator:
    def __init__(self, seed: int, spans_target: int) -> None:
        self.rng = random.Random(seed)  # noqa: S311 - deterministic test data
        self.target = spans_target

    def _text(self, n: int) -> str:
        return " ".join(self.rng.choice(WORDS) for _ in range(n))

    def runs(self) -> Iterator[list[NormalizedSpan]]:
        rng = self.rng
        produced = 0
        session_seq = 0
        while produced < self.target:
            session_seq += 1
            session = SessionKey(f"conv-{session_seq:05d}")
            t = BASE_NS + rng.randrange(DAYS * 86_400 * NS)
            source = rng.choice(SOURCES)
            agent = rng.choice(AGENTS)
            for _ in range(rng.randint(1, 5)):
                if produced >= self.target:
                    break
                run = self._run(t, source, agent, session)
                produced += len(run)
                t = max(sp.end_ns for sp in run) + rng.randint(5, 600) * NS
                yield run

    def _span(self, **kw: object) -> NormalizedSpan:
        base: dict[str, object] = {
            "mapping_version": MAPPING_VERSION,
            "received_at_ns": RECEIVED_NS,
            "size_bytes": 200,
            "is_genai": True,
        }
        source = kw["source"]
        assert isinstance(source, SourceKey)
        base["resource"] = resource_for(source)
        return NormalizedSpan(**(base | kw))  # type: ignore[arg-type]

    def _run(
        self, t0: int, source: SourceKey, agent: str, session: SessionKey
    ) -> list[NormalizedSpan]:
        rng = self.rng
        trace_id = rng.randbytes(16)
        root_id = rng.randbytes(8)
        spans: list[NormalizedSpan] = []
        t = t0 + rng.randint(1, 50) * 1_000_000
        for i in range(rng.randint(2, 4)):
            node = rng.choice(NODES)
            node_id = rng.randbytes(8)
            ns = t
            children: list[NormalizedSpan] = []
            plan = ["llm"] * rng.randint(1, 2) + ["tool"] * rng.randint(0, 2) + ["generic"]
            for kind in plan:
                d = rng.randint(20, 4_000) * 1_000_000
                if kind == "llm":
                    children.append(self._llm(trace_id, node_id, source, agent, t, d))
                elif kind == "tool":
                    if rng.random() < 0.1:
                        children.append(self._approval(trace_id, node_id, source, t))
                        t = children[-1].end_ns
                    children.append(self._tool(trace_id, node_id, source, t, d))
                else:
                    children.append(
                        self._span(
                            trace_id=trace_id, span_id=rng.randbytes(8), parent_span_id=node_id,
                            source=source, name=f"chain {node}", category="generic_genai",
                            start_ns=t, end_ns=t + d // 10, attributes={"langgraph_node": node},
                            search_doc=f"chain {node}",
                        )
                    )  # fmt: skip
                t = children[-1].end_ns + rng.randint(1, 20) * 1_000_000
            err = any(c.status_code == 2 for c in children) and rng.random() < 0.5
            spans.append(
                self._span(
                    trace_id=trace_id, span_id=node_id, parent_span_id=root_id, source=source,
                    name=node, category="generic_genai", start_ns=ns, end_ns=t,
                    status_code=2 if err else 0,
                    attributes={"langgraph_node": node, "langgraph_step": i + 1},
                    session=session if i == 0 else None,
                    graph_node=GraphNodeRow(
                        node_key=node, node_name=node, node_kind="node", start_ns=ns, end_ns=t,
                        status="error" if err else "ok", step=i + 1, agent_name=agent,
                    ),
                    search_doc=node,
                )
            )  # fmt: skip
            spans.extend(children)
        root_err = rng.random() < 0.1
        usage = rng.random() < 0.2
        root = self._span(
            trace_id=trace_id, span_id=root_id, parent_span_id=None, source=source,
            name=f"invoke_agent {agent}", category="agent", start_ns=t0, end_ns=t,
            status_code=2 if root_err else 1,
            status_message="agent failed: max turns exceeded" if root_err else None,
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": agent,
                "gen_ai.conversation.id": session.external_id,
            },
            agent_name=agent, session=session,
            agent_usage_input=1000 if usage else None, agent_usage_output=100 if usage else None,
            graph_node=GraphNodeRow(
                node_key=agent, node_name=agent, node_kind="agent", start_ns=t0, end_ns=t,
                status="error" if root_err else "ok", agent_name=agent,
            ),
            search_doc=f"invoke_agent {agent}" + (" max turns exceeded" if root_err else ""),
        )  # fmt: skip
        return [root, *spans]

    def _llm(
        self, trace_id: bytes, parent: bytes, source: SourceKey, agent: str, t: int, d: int
    ) -> NormalizedSpan:
        rng = self.rng
        provider, model, p_in, p_out, p_c = rng.choice(MODELS)
        it, ot = rng.randint(200, 12_000), rng.randint(20, 1_500)
        ct = rng.randint(0, it // 2) if rng.random() < 0.4 else 0
        err = rng.random() < 0.05
        cost = (
            None
            if p_in is None or p_out is None
            else llm_call_cost_nusd(it, ot, ct, p_in=p_in, p_out=p_out, p_cached=p_c)
        )
        prompt = self._text(rng.randint(5, 20))
        return self._span(
            trace_id=trace_id, span_id=rng.randbytes(8), parent_span_id=parent, source=source,
            name=f"chat {model}", category="llm", start_ns=t, end_ns=t + d,
            status_code=2 if err else 0, status_message="rate limited" if err else None,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": it,
                "gen_ai.usage.output_tokens": ot,
            },
            agent_name=agent,
            llm_call=LLMCallRow(
                start_ns=t, duration_ns=d, operation="chat", unpriced=cost is None,
                provider=provider, request_model=model, response_model=model, model=model,
                input_tokens=it, output_tokens=ot, cached_input_tokens=ct, cost_nusd=cost,
                status="error" if err else "ok", error_type="429" if err else None,
                finish_reasons=["stop"], params={"temperature": 0.2},
            ),
            search_doc=f"chat {model} {prompt}" + (" rate limited" if err else ""),
        )  # fmt: skip

    def _tool(
        self, trace_id: bytes, parent: bytes, source: SourceKey, t: int, d: int
    ) -> NormalizedSpan:
        rng = self.rng
        tool = rng.choice(TOOLS)
        err = rng.random() < 0.1
        args = self._text(rng.randint(2, 6))
        return self._span(
            trace_id=trace_id, span_id=rng.randbytes(8), parent_span_id=parent, source=source,
            name=f"execute_tool {tool}", category="tool", start_ns=t, end_ns=t + d,
            status_code=2 if err else 0,
            status_message=f"{tool} failed: upstream timeout" if err else None,
            attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool},
            tool_call=ToolCallRow(
                start_ns=t, duration_ns=d, status="error" if err else "ok", tool_name=tool,
                tool_call_id=f"call_{rng.getrandbits(48):012x}", tool_type="function",
                error_type="TimeoutError" if err else None,
            ),
            search_doc=f"execute_tool {tool} {args}",
        )  # fmt: skip

    def _approval(
        self, trace_id: bytes, parent: bytes, source: SourceKey, t: int
    ) -> NormalizedSpan:
        rng = self.rng
        decision = rng.choice(["approved", "rejected", "edited", "timeout"])
        wait = rng.randint(2, 900) * NS
        return self._span(
            trace_id=trace_id, span_id=rng.randbytes(8), parent_span_id=parent, source=source,
            name="approval", category="approval", start_ns=t, end_ns=t + wait,
            attributes={"reflex_agentkit.approval.decision": decision},
            approval=ApprovalRow(
                kind="approval", decision=decision, requested_ns=t,  # type: ignore[arg-type]
                approver="ops", decided_ns=t + wait, wait_ns=wait,
            ),
        )  # fmt: skip


def dataset(seed: int, spans_target: int) -> list[list[NormalizedSpan]]:
    """Runs (lists of spans in generation order) totalling at least ``spans_target`` spans."""
    return list(Generator(seed, spans_target).runs())
