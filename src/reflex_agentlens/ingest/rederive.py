"""Re-derive a stored span for ``rebuild`` (plan task P5-03.3; REQ-042.a, 035, 061).

:func:`rederive` is the :class:`~reflex_agentlens.store.records.SpanDeriver` the CLI hands to
``StorageBackend.rebuild``. It runs the derivation half of the ingest pipeline
(``normalize_span`` after redaction) on the span **as stored**:

- classification, LLM call, tool call, approval, graph node, agent name and usage, and the
  search document come from the current mapping table (``MAPPING_VERSION``);
- the LLM call is priced with the given version, the one in effect now ("current pricing"),
  whatever the span's start time;
- stored content was redacted, truncated and possibly dropped at ingest. It is not processed
  again (the raw values are gone), and the Session key, computed from pre-redaction values, is
  kept as stored. Identity, times, raw fields, size, flags and drop records are kept too.
"""

from __future__ import annotations

import dataclasses
from typing import cast

from reflex_agentlens.core.semconv.mapping import MAPPING_VERSION, resolve
from reflex_agentlens.ingest.cost import PricingSnapshot, price_call
from reflex_agentlens.ingest.normalize.approval import approval
from reflex_agentlens.ingest.normalize.classify import classify_draft
from reflex_agentlens.ingest.normalize.common import text
from reflex_agentlens.ingest.normalize.graph import graph_node
from reflex_agentlens.ingest.normalize.llm import agent_usage, llm_call
from reflex_agentlens.ingest.normalize.search import search_document
from reflex_agentlens.ingest.normalize.spans import SpanDraft
from reflex_agentlens.ingest.normalize.tool import tool_call
from reflex_agentlens.store.attrs import JsonValue
from reflex_agentlens.store.records import NormalizedSpan, PricingVersion

__all__ = ["rederive"]


class _Current(PricingSnapshot):
    """One version, applied at every timestamp."""

    def __init__(self, version: PricingVersion) -> None:
        super().__init__((version,))
        self.version = version

    def at(self, t_ns: int) -> PricingVersion | None:
        return self.version


def _draft(span: NormalizedSpan) -> SpanDraft:
    return SpanDraft(
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        name=span.name,
        otel_kind=span.otel_kind,
        start_ns=span.start_ns,
        end_ns=span.end_ns,
        status_code=span.status_code,
        status_message=span.status_message,
        attributes=dict(span.attributes),
        events=[cast(dict[str, JsonValue], e) for e in span.events],
        links=[cast(dict[str, JsonValue], link) for link in span.links],
        resource=dict(span.resource.attributes),
        resource_schema_url=span.resource.schema_url,
        scope_name=span.scope_name,
        scope_version=span.scope_version,
        received_at_ns=span.received_at_ns,
        dropped_counts=dict(span.dropped_counts),
        dropped_keys=list(span.dropped_keys),
        flags=span.flags,
    )


def rederive(span: NormalizedSpan, pricing: PricingVersion, /) -> NormalizedSpan:
    """The stored span with every derived field recomputed (``SpanDeriver``)."""
    d = _draft(span)
    cls = classify_draft(d)
    llm = llm_call(d, cls)
    usage_in, usage_out = agent_usage(d, cls)
    return dataclasses.replace(
        span,
        mapping_version=MAPPING_VERSION,
        category=cls.category,
        is_genai=cls.is_genai,
        agent_name=text(resolve(d.attributes, "agent_name"), 256),
        agent_usage_input=usage_in,
        agent_usage_output=usage_out,
        llm_call=None if llm is None else price_call(llm, _Current(pricing)),
        tool_call=tool_call(d, cls),
        approval=approval(d),
        graph_node=graph_node(d, cls),
        search_doc=search_document(d, cls),
    )
