"""Spike S2a: seeded synthetic dataset generator (Task P0-07.2, prototype of P8-05).

Produces already-normalized span records (what the ingest normalizer would hand to the store):
Runs of ~20 spans (agent root, graph nodes, LLM calls with tokens and cost, tool calls with some
errors, a few approvals), Sessions of 1-10 Runs, 10 models, 30 tools, 5 Sources, over 30 days.
Same seed -> same records (``random.Random`` only, no time/uuid sources).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterator
from dataclasses import dataclass, field

NS = 1_000_000_000
BASE_NS = 1_790_812_800 * NS  # 2026-10-01T00:00:00Z
DAYS = 30
MAPPING_VERSION = 1

SOURCES = [
    ("shop", "support-agent"),
    ("shop", "refund-agent"),
    ("", "research-bot"),
    ("", "unknown_service"),
    ("lab", "eval-runner"),
]
AGENTS = ["triage", "refunds", "researcher", "planner", "writer"]
# (provider, model, input $/Mtok, output $/Mtok, cached $/Mtok) in nano-USD per Mtok
MODELS = [
    ("openai", "gpt-4o", 2_500_000_000, 10_000_000_000, 1_250_000_000),
    ("openai", "gpt-4o-mini", 150_000_000, 600_000_000, 75_000_000),
    ("openai", "o3-mini", 1_100_000_000, 4_400_000_000, 550_000_000),
    ("anthropic", "claude-sonnet-5-5", 3_000_000_000, 15_000_000_000, 300_000_000),
    ("anthropic", "claude-haiku-5-5", 1_000_000_000, 5_000_000_000, 100_000_000),
    ("google", "gemini-2.5-pro", 1_250_000_000, 10_000_000_000, None),
    ("google", "gemini-2.5-flash", 300_000_000, 2_500_000_000, None),
    ("mistral", "mistral-large", 2_000_000_000, 6_000_000_000, None),
    ("ollama", "llama3.3:70b", None, None, None),  # unpriced
    ("openai", "text-embedding-3-small", 20_000_000, 0, None),
]
TOOLS = [
    "search_web",
    "db.query",
    "RefundTool",
    "get_order",
    "send_email",
    "lookup_customer",
    "create_ticket",
    "close_ticket",
    "fetch_url",
    "read_file",
    "write_file",
    "run_python",
    "calculator",
    "get_weather",
    "translate",
    "summarize_doc",
    "vector_search",
    "sql.execute",
    "http.get",
    "http.post",
    "calendar.create",
    "calendar.list",
    "slack.post",
    "jira.create",
    "github.search",
    "github.pr",
    "kb-lookup",
    "price-check",
    "inventory-check",
    "geo-code",
]
NODES = ["router", "retrieve", "plan", "act", "reflect", "respond", "verify", "escalate"]
WORDS = (
    "order refund customer weather report invoice delayed shipment password reset account "
    "billing error timeout café naïve résumé policy warranty upgrade cancel subscription "
    "invoice tracking label address payment declined chargeback escalation summary"
).split()


@dataclass(slots=True)
class SpanRec:
    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes | None
    source: int
    name: str
    category: str
    start_ns: int
    end_ns: int
    status_code: int = 0
    status_message: str | None = None
    is_genai: bool = True
    attributes: dict = field(default_factory=dict)
    agent_name: str | None = None
    session_ext: str | None = None
    agent_usage_input: int | None = None
    agent_usage_output: int | None = None
    llm: dict | None = None
    tool: dict | None = None
    approval: dict | None = None
    node: dict | None = None
    doc: str = ""


def cost_nusd(model_idx: int, it: int, ot: int, ct: int) -> int | None:
    _, _, p_in, p_out, p_c = MODELS[model_idx]
    if p_in is None:
        return None
    num = (it - ct) * p_in + ct * (p_c if p_c is not None else p_in) + ot * p_out
    q, r = divmod(num, 1_000_000)
    if r * 2 > 1_000_000 or (r * 2 == 1_000_000 and q % 2):  # round-half-even
        q += 1
    return q


class Generator:
    def __init__(self, seed: int, spans_target: int, big_trace: int = 0) -> None:
        self.rng = random.Random(seed)
        self.target = spans_target
        self.big_trace = big_trace

    def _text(self, n: int) -> str:
        return " ".join(self.rng.choice(WORDS) for _ in range(n))

    def runs(self) -> Iterator[list[SpanRec]]:
        """Yield Runs (lists of spans in generation order) until ``spans_target`` is reached."""
        rng = self.rng
        produced = 0
        session_seq = 0
        while produced < self.target:
            session_seq += 1
            session_ext = f"conv-{session_seq:07d}"
            n_runs = rng.randint(1, 10)
            t = BASE_NS + rng.randrange(DAYS * 86_400 * NS)
            source = rng.randrange(len(SOURCES))
            agent = rng.choice(AGENTS)
            for _ in range(n_runs):
                if produced >= self.target:
                    break
                big = self.big_trace if (self.big_trace and produced == 0) else 0
                run = self._run(t, source, agent, session_ext, big)
                produced += len(run)
                t = run[0].end_ns + rng.randint(5, 600) * NS
                yield run

    def _run(self, t0: int, source: int, agent: str, session_ext: str, big: int) -> list[SpanRec]:
        rng = self.rng
        trace_id = rng.randbytes(16)
        spans: list[SpanRec] = []
        root_id = rng.randbytes(8)
        t = t0 + rng.randint(1, 50) * 1_000_000
        n_nodes = rng.randint(3, 5) if not big else big // 5
        for i in range(n_nodes):
            node = rng.choice(NODES)
            node_id = rng.randbytes(8)
            ns = t
            children: list[SpanRec] = []
            # child spans: 1-3 LLM calls, 0-2 tools, 1 generic
            plan = ["llm"] * rng.randint(1, 3) + ["tool"] * rng.randint(0, 2) + ["generic"]
            if big:
                plan = ["llm", "tool", "generic", "llm"]
            for kind in plan:
                d = rng.randint(20, 4_000) * 1_000_000
                if kind == "llm":
                    children.append(self._llm(trace_id, node_id, source, agent, t, d))
                elif kind == "tool":
                    if rng.random() < 0.05:
                        children.append(self._approval(trace_id, node_id, source, t))
                        t = children[-1].end_ns
                    children.append(self._tool(trace_id, node_id, source, t, d))
                else:
                    children.append(
                        SpanRec(
                            trace_id,
                            rng.randbytes(8),
                            node_id,
                            source,
                            f"chain {node}",
                            "generic_genai",
                            t,
                            t + d // 10,
                            attributes={"langgraph_node": node},
                            doc=f"chain {node}",
                        )
                    )
                t = children[-1].end_ns + rng.randint(1, 20) * 1_000_000
            err = any(c.status_code == 2 for c in children) and rng.random() < 0.5
            node_span = SpanRec(
                trace_id,
                node_id,
                root_id,
                source,
                node,
                "generic_genai",
                ns,
                t,
                status_code=2 if err else 0,
                attributes={"langgraph_node": node, "langgraph_step": i + 1},
                session_ext=session_ext if i == 0 else None,
                node={
                    "node_key": node,
                    "node_name": node,
                    "node_kind": "node",
                    "step": i + 1,
                    "agent_name": agent,
                    "status": "error" if err else "ok",
                },
                doc=node,
            )
            spans.append(node_span)
            spans.extend(children)
        root_err = rng.random() < 0.03
        root = SpanRec(
            trace_id,
            root_id,
            None,
            source,
            f"invoke_agent {agent}",
            "agent",
            t0,
            t,
            status_code=2 if root_err else 1,
            status_message="agent failed: max turns exceeded" if root_err else None,
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": agent,
                "gen_ai.conversation.id": session_ext,
                "deployment.environment": "prod",
                "user.tier": rng.choice(["free", "pro", "enterprise"]),
            },
            agent_name=agent,
            session_ext=session_ext,
            node={
                "node_key": agent,
                "node_name": agent,
                "node_kind": "agent",
                "step": None,
                "agent_name": agent,
                "status": "error" if root_err else "ok",
            },
            doc=f"invoke_agent {agent}" + (" agent failed: max turns exceeded" if root_err else ""),
        )
        return [root, *spans]

    def _llm(self, trace_id, parent, source, agent, t, d) -> SpanRec:
        rng = self.rng
        mi = rng.choices(range(len(MODELS)), weights=[20, 25, 5, 15, 10, 8, 8, 4, 3, 2])[0]
        provider, model, *_ = MODELS[mi]
        it = rng.randint(200, 12_000)
        ot = rng.randint(20, 1_500) if mi != 9 else 0
        ct = rng.randint(0, it // 2) if rng.random() < 0.4 else 0
        err = rng.random() < 0.01
        prompt, answer = self._text(rng.randint(15, 60)), self._text(rng.randint(10, 50))
        cost = cost_nusd(mi, it, ot, ct)
        op = "embeddings" if mi == 9 else "chat"
        attrs = {
            "gen_ai.operation.name": op,
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            "gen_ai.usage.input_tokens": it,
            "gen_ai.usage.output_tokens": ot,
            "gen_ai.input.messages": [
                {"role": "user", "parts": [{"type": "text", "content": prompt}]}
            ],
            "gen_ai.output.messages": [
                {"role": "assistant", "parts": [{"type": "text", "content": answer}]}
            ],
        }
        return SpanRec(
            trace_id,
            rng.randbytes(8),
            parent,
            source,
            f"{op} {model}",
            "llm",
            t,
            t + d,
            status_code=2 if err else 0,
            status_message="rate limited" if err else None,
            attributes=attrs,
            agent_name=agent,
            llm={
                "operation": op,
                "provider": provider,
                "request_model": model,
                "response_model": model,
                "model": model,
                "input_tokens": it,
                "output_tokens": ot,
                "cached_input_tokens": ct,
                "cache_write_tokens": None,
                "reasoning_tokens": None,
                "cost_nusd": cost,
                "unpriced": cost is None,
                "pricing_version": None if cost is None else 1,
                "status": "error" if err else "ok",
                "error_type": "429" if err else None,
                "finish_reasons": ["stop"],
                "params": {"temperature": 0.2},
            },
            doc=f"{op} {model} {prompt} {answer}" + (" rate limited" if err else ""),
        )

    def _tool(self, trace_id, parent, source, t, d) -> SpanRec:
        rng = self.rng
        tool = TOOLS[min(int(rng.paretovariate(1.2)) - 1, len(TOOLS) - 1)]
        err = rng.random() < 0.08
        args = self._text(rng.randint(3, 12))
        msg = f"{tool} failed: upstream timeout" if err else None
        return SpanRec(
            trace_id,
            rng.randbytes(8),
            parent,
            source,
            f"execute_tool {tool}",
            "tool",
            t,
            t + d,
            status_code=2 if err else 0,
            status_message=msg,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool,
                "gen_ai.tool.call.arguments": {"q": args},
            },
            tool={
                "tool_name": tool,
                "tool_call_id": f"call_{rng.getrandbits(48):012x}",
                "tool_type": "function",
                "status": "error" if err else "ok",
                "error_type": "TimeoutError" if err else None,
            },
            doc=f"execute_tool {tool} {args}" + (f" {msg}" if msg else ""),
        )

    def _approval(self, trace_id, parent, source, t) -> SpanRec:
        rng = self.rng
        decision = rng.choices(["approved", "rejected", "edited", "timeout"], [70, 15, 10, 5])[0]
        wait = rng.randint(2, 900) * NS
        return SpanRec(
            trace_id,
            rng.randbytes(8),
            parent,
            source,
            "approval",
            "approval",
            t,
            t + wait,
            attributes={"reflex_agent.approval.decision": decision},
            approval={
                "kind": "approval",
                "decision": decision,
                "approver": "ops",
                "requested_ns": t,
                "decided_ns": t + wait,
                "wait_ns": wait,
            },
            doc="approval",
        )


def dataset(seed: int, spans_target: int, big_trace: int = 0) -> list[list[SpanRec]]:
    return list(Generator(seed, spans_target, big_trace).runs())


def checksum(runs: list[list[SpanRec]]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for run in runs:
        for s in run:
            h.update(s.trace_id + s.span_id)
    return h.hexdigest()
