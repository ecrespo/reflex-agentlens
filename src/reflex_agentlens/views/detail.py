"""Span drawer view model (plan task P6-06.2; REQ-151, 152, 327).

``SpanDetail`` → display sections: header (name, kind, timing, status, Source), LLM call
(model, parameters, token and cost breakdown, finish reasons), messages as role bubbles, tool
I/O as pretty JSON, error and stack trace, approval, and the raw attributes (built only when the
raw tab is opened). Strings only; Markdown is rendered by ``components/richtext.py``, everything
else is plain text escaped by React.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from reflex_agentlens.query.dto import (
    ApprovalView,
    ErrorInfo,
    LLMCallView,
    Message,
    MessagePart,
    SpanDetail,
    SpanRaw,
    ToolCallView,
)
from reflex_agentlens.views.fmt import DASH, fmt_cost, fmt_duration, fmt_int, fmt_time
from reflex_agentlens.views.tokens import KIND_LABELS

__all__ = [
    "MAX_JSON_CHARS",
    "Bubble",
    "DrawerView",
    "Pair",
    "Part",
    "Section",
    "drawer_view",
    "pretty_json",
    "raw_sections",
]

MAX_JSON_CHARS = 200_000  # one pretty-printed value shown in the drawer


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True)


class Pair(_Row):
    label: str
    value: str


class Part(_Row):
    kind: str  # text | tool_call | tool_result | reasoning | other
    title: str
    body: str
    markdown: bool  # text and reasoning: rich text; the rest: preformatted


class Bubble(_Row):
    role: str
    direction: str
    title: str
    parts: list[Part]


class Section(_Row):
    title: str
    body: str


class DrawerView(_Row):
    span_id: str
    title: str
    kind: str
    kind_label: str
    status: str
    header: list[Pair]
    badges: list[str]
    llm: list[Pair]
    usage: list[Pair]
    messages: list[Bubble]
    content_dropped: bool
    tool: list[Pair]
    tool_arguments: str
    tool_result: str
    error: list[Pair]
    stacktrace: str
    approval: list[Pair]


def pretty_json(value: str | None) -> str:
    """Indented JSON when ``value`` parses; the text as captured otherwise; capped."""
    if value is None:
        return ""
    try:
        text = json.dumps(json.loads(value), indent=2, ensure_ascii=False, sort_keys=True)
    except (ValueError, TypeError):
        text = value
    return _cap(text)


def _cap(text: str) -> str:
    if len(text) <= MAX_JSON_CHARS:
        return text
    return text[:MAX_JSON_CHARS] + f"\n… {len(text) - MAX_JSON_CHARS:,} more characters"


def _dump(value: Any) -> str:
    return _cap(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str))


def _pairs(rows: Sequence[tuple[str, str | None]]) -> list[Pair]:
    return [Pair(label=k, value=v) for k, v in rows if v not in (None, "")]


def _part(part: MessagePart) -> Part:
    if part.type in ("text", "reasoning"):
        title = "Reasoning" if part.type == "reasoning" else ""
        return Part(kind=part.type, title=title, body=part.content or "", markdown=True)
    if part.type == "tool_call":
        title = f"Tool call: {part.name or DASH}" + (f" ({part.id})" if part.id else "")
        return Part(kind="tool_call", title=title, body=pretty_json(part.arguments_json),
                    markdown=False)  # fmt: skip
    if part.type == "tool_result":
        title = "Tool result" + (f" ({part.id})" if part.id else "")
        body = pretty_json(part.result_json) or part.content or ""
        return Part(kind="tool_result", title=title, body=body, markdown=False)
    body = pretty_json(part.raw_json) or part.content or ""
    return Part(kind="other", title=part.raw_type or "Other", body=body, markdown=False)


def _bubble(message: Message) -> Bubble:
    title = message.role.capitalize()
    if message.name:
        title += f" · {message.name}"
    if message.finish_reason:
        title += f" · {message.finish_reason}"
    return Bubble(role=message.role, direction=message.direction, title=title,
                  parts=[_part(p) for p in message.parts])  # fmt: skip


def _params(params: Mapping[str, object]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(params.items()))


def _llm(llm: LLMCallView | None) -> tuple[list[Pair], list[Pair]]:
    if llm is None:
        return [], []
    info = _pairs([
        ("Model", llm.model),
        ("Requested model", llm.request_model if llm.request_model != llm.model else None),
        ("Provider", llm.provider),
        ("Operation", llm.operation),
        ("Parameters", _params(llm.params)),
        ("Finish reasons", ", ".join(llm.finish_reasons)),
        ("Error type", llm.error_type),
    ])  # fmt: skip
    tokens = [
        ("Input tokens", llm.input_tokens),
        ("Cached input tokens", llm.cached_input_tokens),
        ("Cache write tokens", llm.cache_write_tokens),
        ("Output tokens", llm.output_tokens),
        ("Reasoning tokens", llm.reasoning_tokens),
    ]
    usage = _pairs([(k, None if v is None else fmt_int(v)) for k, v in tokens])
    usage.append(Pair(label="Cost", value=fmt_cost(llm.cost_usd, llm.unpriced)))
    if llm.pricing_version is not None:
        usage.append(Pair(label="Pricing version", value=str(llm.pricing_version)))
    return info, usage


def _tool(tool: ToolCallView | None) -> tuple[list[Pair], str, str]:
    if tool is None:
        return [], "", ""
    info = _pairs([
        ("Tool", tool.tool_name),
        ("Call ID", tool.tool_call_id),
        ("Type", tool.tool_type),
        ("Status", tool.status),
        ("Error type", tool.error_type),
    ])  # fmt: skip
    return info, pretty_json(tool.arguments_json), pretty_json(tool.result_json)


def _error(error: ErrorInfo | None) -> tuple[list[Pair], str]:
    if error is None:
        return [], ""
    info = _pairs([("Exception", error.exception_type), ("Message", error.message)])
    return info, _cap(error.stacktrace or "")


def _approval(approval: ApprovalView | None) -> list[Pair]:
    if approval is None:
        return []
    return _pairs([
        ("Kind", approval.kind),
        ("Decision", approval.decision),
        ("Approver", approval.approver),
        ("Tool", approval.tool_name),
        ("Node", approval.node_name),
        ("Requested", fmt_time(approval.requested_ns)),
        ("Decided", fmt_time(approval.decided_ns) if approval.decided_ns else None),
        ("Waited", fmt_duration(approval.wait_ns) if approval.wait_ns is not None else None),
    ])  # fmt: skip


def drawer_view(detail: SpanDetail) -> DrawerView:
    span = detail.span
    llm_info, usage = _llm(detail.llm)
    tool_info, args, result = _tool(detail.tool)
    error_info, stack = _error(detail.error)
    badges = [name for name, on in (("redacted", span.redacted), ("truncated", span.truncated),
              ("content dropped", span.content_dropped),
              ("missing parent", span.missing_parent)) if on]  # fmt: skip
    status = "error" if span.status == "error" else ("ok" if span.status == "ok" else "unset")
    header = _pairs([
        ("Span ID", span.span_id),
        ("Start (UTC)", fmt_time(span.start_ns)),
        ("Duration", fmt_duration(span.duration_ns)),
        ("Status", status),
        ("Status message", span.status_message),
        ("Source", span.source.label if span.source else None),
        ("Agent", span.agent_name),
    ])  # fmt: skip
    messages = detail.llm.messages if detail.llm else []
    dropped = bool(span.content_dropped or (detail.llm and detail.llm.content_dropped)
                   or (detail.tool and detail.tool.content_dropped))  # fmt: skip
    return DrawerView(
        span_id=span.span_id,
        title=span.label or span.name or DASH,
        kind=span.kind,
        kind_label=KIND_LABELS[span.kind],
        status=status,
        header=header,
        badges=badges,
        llm=llm_info,
        usage=usage,
        messages=[_bubble(m) for m in messages],
        content_dropped=dropped,
        tool=tool_info,
        tool_arguments=args,
        tool_result=result,
        error=error_info,
        stacktrace=stack,
        approval=_approval(span.approval),
    )


def raw_sections(raw: SpanRaw) -> list[Section]:
    """The raw tab (REQ-151.e): attributes, resource, scope, events, links, dropped keys."""
    sections = [
        Section(title="Attributes", body=_dump(raw.attributes)),
        Section(title="Resource", body=_dump(raw.resource)),
    ]
    if raw.scope_name:
        scope = raw.scope_name + (f" {raw.scope_version}" if raw.scope_version else "")
        sections.append(Section(title="Instrumentation scope", body=scope))
    if raw.events:
        sections.append(Section(title="Events", body=_dump([e.model_dump() for e in raw.events])))
    if raw.links:
        sections.append(Section(title="Links", body=_dump([lk.model_dump() for lk in raw.links])))
    if raw.dropped_keys:
        sections.append(Section(title="Dropped keys", body="\n".join(raw.dropped_keys)))
    return sections
