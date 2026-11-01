"""The pure ingest pipeline for one export request (plan task P3-03.3; Tech Design Flow 1 step 9).

Per span, in this order (Tech Design v1.4, Analyze A-07):

    validate (incl. clock skew and retention) → redaction (rules, then hook) → truncate long
    values → content drop (if the Project disabled content) → derive LLMCall / ToolCall /
    Approval / GraphNode → cost (pricing snapshot) → search document

Invalid spans and spans whose redaction hook failed are counted per reason and left out; the rest
form one :class:`NormalizedBatch`. No I/O happens here: the Project's settings, the pricing
snapshot, the hook and the Session secret come in through :class:`BatchContext`, so the function
is deterministic for a given request and context.
"""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from reflex_agentlens.core.semconv.mapping import MAPPING_VERSION, resolve
from reflex_agentlens.ingest.cost import PricingSnapshot, price_call
from reflex_agentlens.ingest.normalize.approval import approval
from reflex_agentlens.ingest.normalize.classify import classify_draft
from reflex_agentlens.ingest.normalize.common import text
from reflex_agentlens.ingest.normalize.content import drop_content
from reflex_agentlens.ingest.normalize.graph import graph_node
from reflex_agentlens.ingest.normalize.llm import agent_usage, llm_call
from reflex_agentlens.ingest.normalize.search import search_document
from reflex_agentlens.ingest.normalize.session import session_key
from reflex_agentlens.ingest.normalize.spans import (
    SpanDraft,
    draft,
    resource_in,
    size_bytes,
    source_of,
    truncate,
)
from reflex_agentlens.ingest.normalize.tool import tool_call
from reflex_agentlens.ingest.redaction.engine import redact_draft
from reflex_agentlens.ingest.redaction.hook import HookFailed, RedactionHook, run_hook
from reflex_agentlens.ingest.redaction.rules import CompiledRules
from reflex_agentlens.ingest.validate import Rejections, ValidationLimits, validate_span
from reflex_agentlens.store.records import NormalizedBatch, NormalizedSpan

__all__ = ["BatchContext", "NormalizeResult", "normalize_batch", "normalize_span"]


@dataclass(frozen=True, slots=True)
class BatchContext:
    """Everything the pipeline needs besides the request (held in memory by the engine)."""

    project_id: str
    received_at_ns: int
    rules: CompiledRules
    session_secret: bytes
    pricing: PricingSnapshot
    content_storage: bool = True
    max_attribute_value_bytes: int = 256 << 10
    max_clock_skew_ns: int = 3_600 * 1_000_000_000
    retention_max_age_s: int = 0
    hook: RedactionHook | None = None

    @property
    def limits(self) -> ValidationLimits:
        return ValidationLimits(
            self.received_at_ns, self.max_clock_skew_ns, self.retention_max_age_s
        )


@dataclass(frozen=True, slots=True)
class NormalizeResult:
    batch: NormalizedBatch
    rejections: Rejections


def normalize_span(raw: SpanDraft, ctx: BatchContext) -> NormalizedSpan:
    """Redaction onwards for one validated span; ``HookFailed`` if the hook rejects it."""
    redacted = redact_draft(raw, ctx.rules)
    if ctx.hook is not None:
        redacted = run_hook(redacted, ctx.hook)
    session = session_key(raw.attributes, redacted.attributes, ctx.session_secret)
    d = truncate(redacted, ctx.max_attribute_value_bytes)
    if not ctx.content_storage:
        d = drop_content(d)
    cls = classify_draft(d)
    llm = llm_call(d, cls)
    usage_in, usage_out = agent_usage(d, cls)
    return NormalizedSpan(
        trace_id=d.trace_id,
        span_id=d.span_id,
        parent_span_id=d.parent_span_id,
        source=source_of(d.resource),
        resource=resource_in(d),
        name=d.name[:1024],
        start_ns=d.start_ns,
        end_ns=d.end_ns,
        mapping_version=MAPPING_VERSION,
        size_bytes=size_bytes(d),
        received_at_ns=d.received_at_ns,
        otel_kind=d.otel_kind,
        category=cls.category,
        is_genai=cls.is_genai,
        status_code=d.status_code,
        status_message=d.status_message,
        scope_name=None if d.scope_name is None else d.scope_name[:256],
        scope_version=None if d.scope_version is None else d.scope_version[:64],
        attributes=d.attributes,
        events=list(d.events),
        links=list(d.links),
        dropped_counts=d.dropped_counts,
        dropped_keys=d.dropped_keys,
        agent_name=text(resolve(d.attributes, "agent_name"), 256),
        session=session,
        agent_usage_input=usage_in,
        agent_usage_output=usage_out,
        flags=d.flags,
        llm_call=None if llm is None else price_call(llm, ctx.pricing),
        tool_call=tool_call(d, cls),
        approval=approval(d),
        graph_node=graph_node(d, cls),
        search_doc=search_document(d, cls),
    )


def normalize_batch(request: ExportTraceServiceRequest, ctx: BatchContext) -> NormalizeResult:
    """The whole pure pipeline for one decoded request (spans keep their request order)."""
    rejections = Rejections()
    spans: list[NormalizedSpan] = []
    limits = ctx.limits
    for resource_spans in request.resource_spans:
        schema_url = resource_spans.schema_url
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                reason = validate_span(span, limits)
                if reason is not None:
                    rejections.add(reason)
                    continue
                raw = draft(
                    span,
                    resource_spans.resource,
                    scope_spans.scope,
                    ctx.received_at_ns,
                    scope_spans.schema_url or schema_url,
                )
                try:
                    spans.append(normalize_span(raw, ctx))
                except HookFailed:
                    rejections.add("redaction_hook_error")
    return NormalizeResult(NormalizedBatch(ctx.project_id, tuple(spans)), rejections)
