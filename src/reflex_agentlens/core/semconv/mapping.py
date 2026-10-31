"""The declarative semconv mapping table, ``MAPPING_VERSION = 1`` (plan task P3-01.2).

REQ-035.a/b, REQ-251, DD-009, DD-010, Tech Design §5.3-§5.4 (normative).

Every normalized field is a tuple of **sources in priority order; the first present wins**. Spec
sources come first, so a span that carries both the spec attribute and a mapped alternative gets
the spec value (REQ-035.b). Source kinds:

- :class:`Attr` — one attribute, optionally only when another attribute has a given value;
- :class:`Json` — a value inside a JSON-string attribute (``json(attr, $.a.b)``; an empty path
  means the whole object);
- :class:`Mapped` — an attribute whose value is translated through a small table;
- :class:`Prefixed` — one suffix under several vocabulary prefixes (§5.4 approvals).

OpenInference flattened lists (``flat(prefix)``) are reassembled by :func:`flat`; messages have
their own normalizer (:mod:`.messages`). Changing anything here requires bumping
:data:`MAPPING_VERSION` and a ``rebuild`` (REQ-042).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from reflex_agentlens.core.semconv import constants as k

__all__ = [
    "FIELDS",
    "MAPPING_VERSION",
    "Attr",
    "Category",
    "Classification",
    "Dialect",
    "Json",
    "Mapped",
    "Prefixed",
    "Resolved",
    "Source",
    "classify",
    "flat",
    "has_approval",
    "params",
    "resolve",
    "resolve_with_source",
]

MAPPING_VERSION = 1

Dialect = Literal["spec", "deprecated", "pydantic_ai", "openinference", "langsmith", "reflex"]
Category = Literal["llm", "tool", "agent", "approval", "generic_genai", "generic"]
Attrs: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Attr:
    key: str
    dialect: Dialect = "spec"
    when: tuple[str, str] | None = None  # (key, value) that must also be present

    def get(self, attrs: Attrs) -> Any:
        if self.when is not None and attrs.get(self.when[0]) != self.when[1]:
            return None
        return attrs.get(self.key)


@dataclass(frozen=True, slots=True)
class Json:
    key: str
    path: tuple[str, ...]
    dialect: Dialect

    def get(self, attrs: Attrs) -> Any:
        value = _as_json(attrs.get(self.key))
        for step in self.path:
            if not isinstance(value, dict):
                return None
            value = value.get(step)
        return value


@dataclass(frozen=True, slots=True)
class Mapped:
    key: str
    values: Mapping[str, str]
    dialect: Dialect

    def get(self, attrs: Attrs) -> Any:
        raw = attrs.get(self.key)
        return self.values.get(raw) if isinstance(raw, str) else None


@dataclass(frozen=True, slots=True)
class Prefixed:
    prefixes: tuple[str, ...]
    suffix: str
    dialect: Dialect = "reflex"

    def get(self, attrs: Attrs) -> Any:
        for prefix in self.prefixes:
            value = attrs.get(prefix + self.suffix)
            if value is not None:
                return value
        return None

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(p + self.suffix for p in self.prefixes)


Source: TypeAlias = Attr | Json | Mapped | Prefixed


@dataclass(frozen=True, slots=True)
class Resolved:
    value: Any
    source: Source


def _as_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


# OpenInference / LangSmith span kinds → GenAI operation names
_OI_OPERATION = {
    "LLM": k.OP_CHAT,
    "EMBEDDING": k.OP_EMBEDDINGS,
    "TOOL": k.OP_EXECUTE_TOOL,
    "AGENT": k.OP_INVOKE_AGENT,
    "RETRIEVER": k.OP_RETRIEVAL,
}
_LS_OPERATION = {"llm": k.OP_CHAT, "tool": k.OP_EXECUTE_TOOL, "retriever": k.OP_RETRIEVAL}
_ON_OI_TOOL = (k.OI_SPAN_KIND, "TOOL")

FIELDS: dict[str, tuple[Source, ...]] = {
    "operation": (
        Attr(k.GEN_AI_OPERATION_NAME),
        Mapped(k.OI_SPAN_KIND, _OI_OPERATION, "openinference"),
        Mapped(k.LS_SPAN_KIND, _LS_OPERATION, "langsmith"),
    ),
    "provider": (
        Attr(k.GEN_AI_PROVIDER_NAME),
        Attr(k.GEN_AI_SYSTEM, "deprecated"),
        Attr(k.OI_LLM_PROVIDER, "openinference"),
        Attr(k.OI_LLM_SYSTEM, "openinference"),
    ),
    "request_model": (
        Attr(k.GEN_AI_REQUEST_MODEL),
        Attr(k.OI_LLM_MODEL_NAME, "openinference"),
        Attr(k.LS_MODEL_NAME, "langsmith"),
    ),
    "response_model": (
        Attr(k.GEN_AI_RESPONSE_MODEL),
        Attr(k.OI_LLM_MODEL_NAME, "openinference"),
    ),
    "input_tokens": (
        Attr(k.GEN_AI_USAGE_INPUT_TOKENS),
        Attr(k.GEN_AI_USAGE_PROMPT_TOKENS, "deprecated"),
        Attr(k.OI_TOKEN_PROMPT, "openinference"),
    ),
    "output_tokens": (
        Attr(k.GEN_AI_USAGE_OUTPUT_TOKENS),
        Attr(k.GEN_AI_USAGE_COMPLETION_TOKENS, "deprecated"),
        Attr(k.OI_TOKEN_COMPLETION, "openinference"),
    ),
    "cached_input_tokens": (
        Attr(k.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS),
        Attr(k.OI_TOKEN_CACHE_READ, "openinference"),
    ),
    "cache_write_tokens": (
        Attr(k.GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS),
        Attr(k.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS, "deprecated"),
        Attr(k.OI_TOKEN_CACHE_WRITE, "openinference"),
    ),
    "reasoning_tokens": (Attr(k.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS),),
    "finish_reasons": (Attr(k.GEN_AI_RESPONSE_FINISH_REASONS),),
    "tool_name": (Attr(k.GEN_AI_TOOL_NAME), Attr(k.OI_TOOL_NAME, "openinference")),
    "tool_call_id": (Attr(k.GEN_AI_TOOL_CALL_ID), Attr(k.OI_TOOL_CALL_ID, "openinference")),
    "tool_type": (Attr(k.GEN_AI_TOOL_TYPE),),
    "tool_args": (
        Attr(k.GEN_AI_TOOL_CALL_ARGUMENTS),
        Attr(k.OI_INPUT_VALUE, "openinference", when=_ON_OI_TOOL),
    ),
    "tool_result": (
        Attr(k.GEN_AI_TOOL_CALL_RESULT),
        Attr(k.OI_OUTPUT_VALUE, "openinference", when=_ON_OI_TOOL),
    ),
    "agent_name": (
        Attr(k.GEN_AI_AGENT_NAME),
        Attr(k.PAI_AGENT_NAME, "pydantic_ai"),
        Attr(k.OI_AGENT_NAME, "openinference"),
        Attr(k.OI_GRAPH_NODE_NAME, "openinference"),
    ),
    "workflow_name": (Attr(k.GEN_AI_WORKFLOW_NAME),),
    "session_id": (
        Attr(k.GEN_AI_CONVERSATION_ID),
        Attr(k.OI_SESSION_ID, "openinference"),
        Attr(k.LS_THREAD_ID, "langsmith"),
        Attr(k.LS_SESSION_ID, "langsmith"),
        Attr(k.LS_CONVERSATION_ID, "langsmith"),
    ),
    "graph_node_id": (
        Attr(k.OI_GRAPH_NODE_ID, "openinference"),
        Json(k.OI_METADATA, ("langgraph_node",), "openinference"),
    ),
    "graph_node_parent_id": (Attr(k.OI_GRAPH_NODE_PARENT_ID, "openinference"),),
    "graph_step": (Json(k.OI_METADATA, ("langgraph_step",), "openinference"),),
    "error_type": (Attr(k.ERROR_TYPE),),  # + span status ERROR with an exception event (P3-03)
    "approval_id": (Prefixed(k.APPROVAL_PREFIXES, k.APPROVAL_ID_SUFFIX),),
    "approval_decision": (Prefixed(k.APPROVAL_PREFIXES, k.APPROVAL_DECISION_SUFFIX),),
    "approval_approver": (Prefixed(k.APPROVAL_PREFIXES, k.APPROVAL_APPROVER_SUFFIX),),
    "approval_tool_call_id": (Prefixed(k.APPROVAL_PREFIXES, k.APPROVAL_TOOL_CALL_ID_SUFFIX),),
    "deferral_name": (Attr(k.PAI_TOOL_DEFERRAL_NAME, "pydantic_ai"),),
}

_PARAM_KEYS = {
    k.GEN_AI_REQUEST_TEMPERATURE: "temperature",
    k.GEN_AI_REQUEST_MAX_TOKENS: "max_tokens",
    k.GEN_AI_REQUEST_TOP_P: "top_p",
    k.GEN_AI_REQUEST_TOP_K: "top_k",
    k.GEN_AI_REQUEST_FREQUENCY_PENALTY: "frequency_penalty",
    k.GEN_AI_REQUEST_PRESENCE_PENALTY: "presence_penalty",
    k.GEN_AI_REQUEST_SEED: "seed",
    k.GEN_AI_REQUEST_STREAM: "stream",
}
PARAM_SOURCES: tuple[Source, ...] = (
    Json(k.OI_INVOCATION_PARAMETERS, (), "openinference"),
    Json(k.PAI_MODEL_REQUEST_PARAMETERS, (), "pydantic_ai"),
)


def resolve_with_source(attrs: Attrs, name: str) -> Resolved | None:
    """The first present source of field ``name`` (``None`` and ``""`` count as absent)."""
    for source in FIELDS[name]:
        value = source.get(attrs)
        if value is not None and value != "":
            return Resolved(value, source)
    return None


def resolve(attrs: Attrs, name: str) -> Any:
    found = resolve_with_source(attrs, name)
    return None if found is None else found.value


def params(attrs: Attrs) -> dict[str, Any]:
    """Request parameters: the spec keys if any is present, else the first JSON parameter object."""
    spec = {short: attrs[key] for key, short in _PARAM_KEYS.items() if key in attrs}
    if spec:
        return spec
    for source in PARAM_SOURCES:
        value = source.get(attrs)
        if isinstance(value, dict) and value:
            return dict(value)
    return {}


_INDEXED = re.compile(r"^(\d+)\.(.+)$")


def flat(attrs: Attrs, prefix: str) -> list[dict[str, Any]]:
    """Reassemble an OpenInference flattened list: ``prefix.<i>.<rest>`` → ``[{rest: value}]``.

    Nested indices stay flattened inside each item (``message.contents.0.message_content.text``);
    the messages normalizer reads them with :func:`flat` again on the item.
    """
    items: dict[int, dict[str, Any]] = {}
    lead = prefix + "."
    for key, value in attrs.items():
        if not key.startswith(lead):
            continue
        match = _INDEXED.match(key[len(lead) :])
        if match is not None:
            items.setdefault(int(match.group(1)), {})[match.group(2)] = value
    return [items[i] for i in sorted(items)]


# ── classification (Tech Design §5.3, first match wins) ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Classification:
    category: Category
    is_genai: bool
    operation: str | None = None
    rule: str = field(default="", compare=False)  # which rule matched (golden tests, debugging)


def has_approval(attrs: Attrs) -> bool:
    return resolve(attrs, "approval_id") is not None or resolve(attrs, "deferral_name") is not None


def _genai_key(key: str) -> bool:
    return key.startswith(("gen_ai.", "llm.", "openinference.", "langsmith.")) or key in (
        k.PAI_AGENT_NAME,
        k.PAI_ALL_MESSAGES,
    )


def classify(attrs: Attrs) -> Classification:
    """Category of one span from its attributes (rules 1-5 of Tech Design §5.3)."""
    op = attrs.get(k.GEN_AI_OPERATION_NAME)
    if isinstance(op, str) and op:
        if op in k.LLM_OPERATIONS:
            return Classification("llm", True, op, "1:operation")
        if op == k.OP_EXECUTE_TOOL:
            return Classification("tool", True, op, "1:operation")
        if op in k.AGENT_OPERATIONS:
            return Classification("agent", True, op, "1:operation")
        if op in (k.OP_RETRIEVAL, k.OP_PLAN) or "_memory" in op or op.startswith("memory"):
            return Classification("generic_genai", True, op, "1:operation")
        # an operation outside the table falls through to rules 2-5
    oi = attrs.get(k.OI_SPAN_KIND)
    if isinstance(oi, str) and oi:
        operation = _OI_OPERATION.get(oi)
        if oi in ("LLM", "EMBEDDING"):
            return Classification("llm", True, operation, "2:openinference")
        if oi == "TOOL":
            return Classification("tool", True, operation, "2:openinference")
        if oi == "AGENT":
            return Classification("agent", True, operation, "2:openinference")
        if oi == "CHAIN" and resolve(attrs, "graph_node_id") is not None:
            return Classification("agent", True, k.OP_INVOKE_AGENT, "2:langgraph-node")
        return Classification("generic_genai", True, operation, "2:openinference")
    ls = attrs.get(k.LS_SPAN_KIND)
    if ls in ("llm", "tool"):
        return Classification("llm" if ls == "llm" else "tool", True, _LS_OPERATION[ls], "3:ls")
    if ls == "chain":
        return Classification("generic_genai", True, None, "3:langsmith")
    op_name = op if isinstance(op, str) and op else None
    if has_approval(attrs):
        return Classification("approval", True, op_name, "4:approval")
    genai = any(_genai_key(key) for key in attrs)
    return Classification("generic_genai" if genai else "generic", genai, op_name, "5:fallback")
