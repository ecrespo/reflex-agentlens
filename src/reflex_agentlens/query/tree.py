"""Span tree of a trace (plan task P4-02.2; REQ-103.a, REQ-037.b, REQ-150.a, REQ-152.a).

Pure functions over ``store.records.TraceSpan``. The tree is returned as a pre-ordered list
(depth-first, siblings by start time then span ID). Spans whose parent never arrived sit under one
placeholder node per missing parent (REQ-037.b); spans caught in a parent cycle (malformed input)
are attached the same way, so every span appears exactly once. Iterative, so a 5,000-deep chain
cannot hit the recursion limit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from reflex_agentlens.core.ids import span_id_to_hex
from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.core.semconv import mapping
from reflex_agentlens.core.semconv import messages as sm
from reflex_agentlens.query import convert, dto
from reflex_agentlens.query.kinds import span_is_genai, span_kind, span_label
from reflex_agentlens.store import records as r

__all__ = [
    "PLACEHOLDER_NAME",
    "SpanTree",
    "build_tree",
    "error_info",
    "llm_messages",
    "page",
    "tool_io",
    "totals",
]

PLACEHOLDER_NAME = "(missing parent span)"
_STATUS = {0: "unset", 1: "ok", 2: "error"}


@dataclass(frozen=True, slots=True)
class SpanTree:
    nodes: list[dto.SpanNode]  # DFS order, placeholders included
    llm_calls: list[dto.LLMCallView]  # start order, messages=[]


def _sort_key(sp: r.TraceSpan) -> tuple[int, bytes]:
    return (sp.start_ns, sp.span_id)


@dataclass(slots=True)
class _Acc:
    tokens: int = 0
    cost_nusd: int = 0
    unpriced: bool = False


def _own(sp: r.TraceSpan) -> _Acc:
    call = sp.llm_call
    if call is None:
        return _Acc()
    return _Acc(
        tokens=(call.input_tokens or 0) + (call.output_tokens or 0),
        cost_nusd=call.cost_nusd or 0,
        unpriced=call.unpriced,
    )


def _node(sp: r.TraceSpan, depth: int, missing_parent: bool, acc: _Acc) -> dto.SpanNode:
    return dto.SpanNode(
        span_id=span_id_to_hex(sp.span_id),
        parent_span_id=None if sp.parent_span_id is None else span_id_to_hex(sp.parent_span_id),
        depth=depth,
        missing_parent=missing_parent,
        name=sp.name,
        label=span_label(
            sp.name, sp.category, llm=sp.llm_call, tool=sp.tool_call, agent_name=sp.agent_name
        ),
        kind=span_kind(sp.category),
        is_genai=span_is_genai(sp.category),
        start_ns=sp.start_ns,
        end_ns=sp.end_ns,
        duration_ns=max(0, sp.end_ns - sp.start_ns),
        status=_STATUS.get(sp.status_code, "unset"),  # type: ignore[arg-type]
        status_message=sp.status_message,
        source=convert.source_ref(sp.source),
        agent_name=sp.agent_name,
        llm=None if sp.llm_call is None else convert.llm_call_view(sp.span_id, sp.llm_call),
        tool=None if sp.tool_call is None else convert.tool_call_view(sp.span_id, sp.tool_call),
        approval=None if sp.approval is None else convert.approval_view(sp.approval),
        subtree_tokens=acc.tokens,
        subtree_cost_usd=convert.money(acc.cost_nusd),
        subtree_includes_unpriced=acc.unpriced,
        truncated=bool(sp.flags & convert.FLAG_TRUNCATED),
        redacted=bool(sp.flags & convert.FLAG_REDACTED),
        content_dropped=bool(sp.flags & convert.FLAG_CONTENT_DROPPED),
    )


def _placeholder(parent: bytes, children: list[r.TraceSpan], acc: _Acc) -> dto.SpanNode:
    start = min(c.start_ns for c in children)
    end = max(c.end_ns for c in children)
    return dto.SpanNode(
        span_id=span_id_to_hex(parent),
        parent_span_id=None,
        depth=0,
        missing_parent=False,
        placeholder=True,
        name=PLACEHOLDER_NAME,
        label=PLACEHOLDER_NAME,
        kind="generic",
        is_genai=False,
        start_ns=start,
        end_ns=end,
        duration_ns=max(0, end - start),
        status="unset",
        status_message=None,
        source=None,
        agent_name=None,
        llm=None,
        tool=None,
        approval=None,
        subtree_tokens=acc.tokens,
        subtree_cost_usd=convert.money(acc.cost_nusd),
        subtree_includes_unpriced=acc.unpriced,
        truncated=False,
        redacted=False,
        content_dropped=False,
    )


@dataclass(frozen=True, slots=True)
class _Entry:
    span: r.TraceSpan | None  # None for a placeholder
    depth: int
    missing_parent: bool
    tree_parent: bytes | None  # parent in the built tree (None for tops)
    placeholder_for: bytes | None = None  # the missing parent ID of a placeholder


def _dfs_order(spans: Sequence[r.TraceSpan]) -> tuple[list[_Entry], dict[bytes, list[r.TraceSpan]]]:
    by_id = {sp.span_id: sp for sp in spans}
    children: dict[bytes, list[r.TraceSpan]] = {}
    roots: list[r.TraceSpan] = []
    orphans: dict[bytes, list[r.TraceSpan]] = {}
    for sp in spans:
        parent = sp.parent_span_id
        if parent is None or parent == sp.span_id:
            roots.append(sp)
        elif parent in by_id:
            children.setdefault(parent, []).append(sp)
        else:
            orphans.setdefault(parent, []).append(sp)
    for group in (*children.values(), *orphans.values()):
        group.sort(key=_sort_key)

    tops: list[tuple[tuple[int, bytes], r.TraceSpan | None, bytes | None]] = [
        (_sort_key(sp), sp, None) for sp in roots
    ]
    tops += [((g[0].start_ns, pid), None, pid) for pid, g in orphans.items()]
    tops.sort(key=lambda x: x[0])
    out: list[_Entry] = []
    seen: set[bytes] = set()

    def walk(top: r.TraceSpan, depth: int, missing: bool, tree_parent: bytes | None) -> None:
        stack = [(top, depth, missing, tree_parent)]
        while stack:
            sp, d, miss, tp = stack.pop()
            if sp.span_id in seen:
                continue
            seen.add(sp.span_id)
            out.append(_Entry(sp, d, miss, tp))
            for child in reversed(children.get(sp.span_id, ())):
                stack.append((child, d + 1, False, sp.span_id))

    for _, top, pid in tops:
        if top is not None:
            walk(top, 0, False, None)
            continue
        assert pid is not None  # noqa: S101 - a top is a root span or a missing parent
        out.append(_Entry(None, 0, False, None, placeholder_for=pid))
        for child in orphans[pid]:
            walk(child, 1, True, pid)
    # spans in a parent cycle are unreachable from any top: break each cycle at its earliest span
    for sp in sorted(spans, key=_sort_key):
        if sp.span_id not in seen:
            walk(sp, 0, True, None)
    return out, orphans


def build_tree(spans: Sequence[r.TraceSpan]) -> SpanTree:
    order, orphans = _dfs_order(spans)
    acc: dict[bytes, _Acc] = {sp.span_id: _own(sp) for sp in spans}
    acc.update({pid: _Acc() for pid in orphans})  # placeholder totals
    for e in reversed(order):
        if e.span is None or e.tree_parent is None:
            continue
        a, b = acc[e.tree_parent], acc[e.span.span_id]
        a.tokens += b.tokens
        a.cost_nusd += b.cost_nusd
        a.unpriced = a.unpriced or b.unpriced
    nodes: list[dto.SpanNode] = []
    for e in order:
        if e.span is None and e.placeholder_for is not None:
            pid = e.placeholder_for
            nodes.append(_placeholder(pid, orphans[pid], acc[pid]))
        elif e.span is not None:
            nodes.append(_node(e.span, e.depth, e.missing_parent, acc[e.span.span_id]))
    llm_calls = [
        convert.llm_call_view(sp.span_id, sp.llm_call)
        for sp in sorted(spans, key=_sort_key)
        if sp.llm_call is not None
    ]
    return SpanTree(nodes=nodes, llm_calls=llm_calls)


# ── totals and the span drawer ───────────────────────────────────────────────────────────────


def totals(run: r.RunSummary) -> dto.TokenCostTotals:
    return dto.TokenCostTotals(
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cached_input_tokens=run.cached_input_tokens,
        total_tokens=run.input_tokens + run.output_tokens,
        cost_usd=convert.money(run.cost_nusd or 0),
        includes_unpriced=run.unpriced_calls > 0,
        unpriced_calls=run.unpriced_calls,
        usage_source=run.usage_source,
    )


def _events(events: Sequence[Any]) -> list[sm.GenAIEvent]:
    out = []
    for ev in events:
        if isinstance(ev, dict):
            attrs = ev.get("attributes")
            out.append(
                sm.GenAIEvent(str(ev.get("name", "")), attrs if isinstance(attrs, dict) else {})
            )
    return out


def llm_messages(attributes: dict[str, Any], events: Sequence[Any]) -> list[dto.Message]:
    """REQ-034 / REQ-104: the normalized conversation from the stored attributes and events."""
    return convert.conversation_messages(sm.extract(attributes, _events(events)))


def tool_io(raw: r.SpanRaw) -> tuple[Any, Any]:
    """REQ-151.c: tool arguments and result through the mapping table."""
    return mapping.resolve(raw.attributes, "tool_args"), mapping.resolve(
        raw.attributes, "tool_result"
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def error_info(
    status_code: int, status_message: str | None, raw: r.SpanRaw
) -> dto.ErrorInfo | None:
    """REQ-151.d: status message plus the first ``exception`` event (type, message, stack)."""
    exc: dict[str, Any] = {}
    for ev in raw.events:
        if isinstance(ev, dict) and ev.get("name") == k.EVENT_EXCEPTION:
            attrs = ev.get("attributes")
            exc = attrs if isinstance(attrs, dict) else {}
            break
    if status_code != 2 and not exc:
        return None
    return dto.ErrorInfo(
        message=status_message or _text(exc.get(k.EXCEPTION_MESSAGE)),
        exception_type=_text(exc.get(k.EXCEPTION_TYPE)) or _text(raw.attributes.get(k.ERROR_TYPE)),
        stacktrace=_text(exc.get(k.EXCEPTION_STACKTRACE)),
    )


def page(
    nodes: list[dto.SpanNode], offset: int, limit: int
) -> tuple[list[dto.SpanNode], int | None]:
    chunk = nodes[offset : offset + limit]
    end = offset + len(chunk)
    return chunk, (end if end < len(nodes) else None)
