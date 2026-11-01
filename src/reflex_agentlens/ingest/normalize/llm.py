"""LLMCall derivation and agent-level usage (plan task P3-03.1; REQ-033.a, REQ-032.d, DD-012).

Only ``llm`` spans get an :class:`LLMCallRow`. Token totals are summed by the store from those
rows (DD-012); an agent span's own usage is kept in ``agent_usage_*`` and only used by the Run
recompute when the trace has no LLM rows. Aggregated usage (``gen_ai.aggregated_usage.*``) and
``gen_ai.usage.total_tokens`` never feed totals. Cost is filled later by the cost stage.
"""

from __future__ import annotations

import json
from typing import Any

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.core.semconv.mapping import Classification, params, resolve
from reflex_agentlens.core.semconv.messages import GenAIEvent, extract
from reflex_agentlens.ingest.normalize.common import content_dropped, count, outcome, text
from reflex_agentlens.ingest.normalize.spans import SpanDraft
from reflex_agentlens.store.records import JsonObject, LLMCallRow

__all__ = ["agent_usage", "events_of", "finish_reasons", "llm_call"]


def events_of(d: SpanDraft) -> list[GenAIEvent]:
    return [
        GenAIEvent(str(ev.get("name", "")), attrs if isinstance(attrs, dict) else {})
        for ev in d.events
        for attrs in (ev.get("attributes"),)
    ]


def _reasons(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                value = json.loads(stripped)
            except (ValueError, RecursionError):
                return [value[:64]]
        else:
            return [value[:64]] if value else []
    if isinstance(value, list):
        return [v[:64] for v in value if isinstance(v, str) and v][:32]
    return []


def finish_reasons(d: SpanDraft) -> list[str]:
    """``gen_ai.response.finish_reasons``, else the per-message ``finish_reason`` (deprecated)."""
    found = _reasons(resolve(d.attributes, "finish_reasons"))
    if found:
        return found
    output = extract(d.attributes, events_of(d)).output
    return [m.finish_reason[:64] for m in output if isinstance(m.finish_reason, str)][:32]


def _params(attrs: dict[str, Any]) -> JsonObject:
    try:
        out = params(attrs)
        json.dumps(out, allow_nan=False)  # stored as JSON: must serialize strictly
    except (TypeError, ValueError, RecursionError):
        return {}
    return out


def llm_call(d: SpanDraft, cls: Classification) -> LLMCallRow | None:
    if cls.category != "llm":
        return None
    attrs = d.attributes
    request_model = text(resolve(attrs, "request_model"), 200)
    response_model = text(resolve(attrs, "response_model"), 200)
    status, error_type = outcome(d)
    operation = cls.operation if cls.operation in k.LLM_OPERATIONS else k.OP_CHAT
    return LLMCallRow(
        start_ns=d.start_ns,
        duration_ns=d.end_ns - d.start_ns,
        operation=operation,
        unpriced=True,  # the cost stage prices it (P3-05)
        provider=text(resolve(attrs, "provider"), 64),
        request_model=request_model,
        response_model=response_model,
        model=response_model or request_model or "unknown",
        input_tokens=count(resolve(attrs, "input_tokens")),
        output_tokens=count(resolve(attrs, "output_tokens")),
        cached_input_tokens=count(resolve(attrs, "cached_input_tokens")),
        cache_write_tokens=count(resolve(attrs, "cache_write_tokens")),
        reasoning_tokens=count(resolve(attrs, "reasoning_tokens")),
        status=status,
        error_type=error_type,
        finish_reasons=finish_reasons(d),
        params=_params(attrs),
        content_dropped=content_dropped(d),
    )


_AGGREGATED_IN = k.GEN_AI_AGGREGATED_USAGE_PREFIX + "input_tokens"
_AGGREGATED_OUT = k.GEN_AI_AGGREGATED_USAGE_PREFIX + "output_tokens"


def agent_usage(d: SpanDraft, cls: Classification) -> tuple[int | None, int | None]:
    """DD-012 fallback input: an agent span's own usage, else its aggregated usage."""
    if cls.category != "agent":
        return None, None
    attrs = d.attributes
    used_in, used_out = (
        count(resolve(attrs, "input_tokens")),
        count(resolve(attrs, "output_tokens")),
    )
    if used_in is None and used_out is None:
        used_in, used_out = count(attrs.get(_AGGREGATED_IN)), count(attrs.get(_AGGREGATED_OUT))
    return used_in, used_out
