"""Trace page view model (plan tasks P6-05.1, P6-05.2; REQ-037.b, 103.b, 131, 150, API §7.2).

- :func:`waterfall_rows`: the span tree as table rows with proportional bars (offset and width
  in % of the Run window; the Art. 7 interpretation of a waterfall), GenAI-aware labels, kind
  colours, collapse beyond depth 3 (a node deeper than that starts collapsed unless expanded),
  placeholder rows for missing parents (REQ-037.b).
- :class:`TraceUrl`: the API §7.2 query of ``/trace/[trace_id]`` (``tab``, ``span``, ``replay``,
  ``speed``, ``project``); decoding never raises.
- :func:`tabs_for`: generic traces get no GenAI panels (REQ-131).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict

from reflex_agentlens.query.dto import ChangeNotice, RunRef, RunSummary, SpanNode, TokenCostTotals
from reflex_agentlens.views.fmt import DASH, fmt_cost, fmt_duration, fmt_int
from reflex_agentlens.views.tokens import KIND_COLORS, KIND_LABELS, STATUS_COLORS

__all__ = [
    "COLLAPSE_DEPTH",
    "TABS",
    "TraceUrl",
    "WaterfallRow",
    "candidate_links",
    "decode_trace_url",
    "encode_trace_url",
    "is_hex_id",
    "needs_reload",
    "tabs_for",
    "toggle",
    "totals_rows",
    "trace_page_href",
    "waterfall_rows",
]

COLLAPSE_DEPTH = 3
INDENT_PX = 16
MIN_BAR_PCT = 0.5  # a zero-length span stays visible
TABS: tuple[str, ...] = ("waterfall", "graph", "cost", "raw")
SPEEDS: tuple[str, ...] = ("1", "2", "5", "step")
Tab = Literal["waterfall", "graph", "cost", "raw"]


class WaterfallRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    span_id: str
    depth: int
    indent_px: int
    label: str
    kind: str
    kind_label: str
    color: str
    offset_pct: float
    width_pct: float
    duration: str
    error: bool
    status_color: str
    placeholder: bool
    missing_parent: bool
    has_children: bool
    collapsed: bool
    tokens: str
    cost: str
    badges: str  # "redacted, truncated", …


def _window(run: RunSummary, spans: Sequence[SpanNode]) -> tuple[int, int]:
    real = [s for s in spans if not s.placeholder]
    start = min([run.start_ns, *(s.start_ns for s in real)])
    ends = [run.end_ns or run.last_span_ns, *(s.end_ns for s in real)]
    end = max(ends)
    return start, max(end, start + 1)


def _badges(span: SpanNode) -> str:
    flags = [
        ("redacted", span.redacted),
        ("truncated", span.truncated),
        ("content dropped", span.content_dropped),
        ("missing parent", span.missing_parent),
    ]
    return ", ".join(name for name, on in flags if on)


def waterfall_rows(
    run: RunSummary,
    spans: Sequence[SpanNode],
    *,
    expanded: frozenset[str] = frozenset(),
    collapsed: frozenset[str] = frozenset(),
) -> list[WaterfallRow]:
    """Visible rows in tree order (the order ``get_trace`` returns)."""
    start, end = _window(run, spans)
    span_ns = end - start
    parents = {s.parent_span_id for s in spans if s.parent_span_id}
    rows: list[WaterfallRow] = []
    hidden_below: int | None = None  # depth of the collapsed ancestor, while inside its subtree
    for span in spans:
        if hidden_below is not None:
            if span.depth > hidden_below:
                continue
            hidden_below = None
        has_children = span.span_id in parents
        is_collapsed = has_children and (
            span.span_id in collapsed
            or (span.depth >= COLLAPSE_DEPTH and span.span_id not in expanded)
        )
        if span.placeholder:
            offset, width = 0.0, 100.0
        else:
            offset = (span.start_ns - start) / span_ns * 100
            width = max((span.end_ns - span.start_ns) / span_ns * 100, MIN_BAR_PCT)
            width = min(width, 100.0 - offset) if offset < 100 else MIN_BAR_PCT
        unpriced_only = span.subtree_includes_unpriced and not span.subtree_cost_usd
        priced = None if unpriced_only else span.subtree_cost_usd
        rows.append(
            WaterfallRow(
                span_id=span.span_id,
                depth=span.depth,
                indent_px=span.depth * INDENT_PX,
                label=span.label or span.name or DASH,
                kind=span.kind,
                kind_label=KIND_LABELS[span.kind],
                color=KIND_COLORS[span.kind],
                offset_pct=round(offset, 3),
                width_pct=round(width, 3),
                duration=DASH if span.placeholder else fmt_duration(span.duration_ns),
                error=span.status == "error",
                status_color=STATUS_COLORS["error" if span.status == "error" else "ok"],
                placeholder=span.placeholder,
                missing_parent=span.missing_parent,
                has_children=has_children,
                collapsed=is_collapsed,
                tokens=fmt_int(span.subtree_tokens) if span.subtree_tokens else "",
                cost=fmt_cost(priced, span.subtree_includes_unpriced) if span.is_genai else "",
                badges=_badges(span),
            )
        )
        if is_collapsed:
            hidden_below = span.depth
    return rows


def toggle(span_id: str, depth: int, expanded: frozenset[str], collapsed: frozenset[str],
           currently_collapsed: bool) -> tuple[frozenset[str], frozenset[str]]:  # fmt: skip
    """Flip one row; returns the new ``(expanded, collapsed)`` sets."""
    if currently_collapsed:
        return expanded | {span_id}, collapsed - {span_id}
    if depth >= COLLAPSE_DEPTH:
        return expanded - {span_id}, collapsed
    return expanded, collapsed | {span_id}


# ── URL ──────────────────────────────────────────────────────────────────────────────────────


def is_hex_id(value: str | None, length: int) -> bool:
    if not value or len(value) != length:
        return False
    return all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True, slots=True)
class TraceUrl:
    tab: Tab = "waterfall"
    span: str | None = None
    replay: str | None = None
    speed: str = "1"
    project: str | None = None


def _first(params: Mapping[str, str | Sequence[str]], key: str) -> str | None:
    value = params.get(key)
    if value is None or isinstance(value, str):
        return value
    return value[0] if value else None


def decode_trace_url(params: Mapping[str, str | Sequence[str]]) -> TraceUrl:
    tab = _first(params, "tab") or "waterfall"
    span = (_first(params, "span") or "").lower() or None
    replay = (_first(params, "replay") or "").lower() or None
    speed = _first(params, "speed") or "1"
    project = _first(params, "project")
    return TraceUrl(
        tab=tab if tab in TABS else "waterfall",  # type: ignore[arg-type]
        span=span if is_hex_id(span, 16) else None,
        replay=replay if is_hex_id(replay, 16) else None,
        speed=speed if speed in SPEEDS else "1",
        project=project if project and len(project) <= 64 and project.isprintable() else None,
    )


def encode_trace_url(url: TraceUrl) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if url.project:
        pairs.append(("project", url.project))
    if url.tab != "waterfall":
        pairs.append(("tab", url.tab))
    if url.span:
        pairs.append(("span", url.span))
    if url.replay:
        pairs.append(("replay", url.replay))
        if url.speed != "1":
            pairs.append(("speed", url.speed))
    return pairs


def tabs_for(run: RunSummary) -> list[str]:
    """REQ-131: a trace without GenAI spans shows no graph or cost panels."""
    return list(TABS) if run.is_genai else ["waterfall", "raw"]


def trace_page_href(prefix: str, trace_id: str, url: TraceUrl) -> str:
    """``<prefix>/trace/<id>`` with the §7.2 query of ``url`` (defaults omitted)."""
    base = f"{prefix.rstrip('/')}/trace/{quote(trace_id, safe='')}"
    query = urlencode(encode_trace_url(url))
    return f"{base}?{query}" if query else base


def candidate_links(refs: Sequence[RunRef], prefix: str, url: TraceUrl) -> list[dict[str, str]]:
    """REQ-103.b: one link per Project when a trace ID matches Runs in several of them."""
    return [
        {"project": r.project_id, "href": trace_page_href(prefix, r.trace_id,
                                                          replace(url, project=r.project_id))}
        for r in refs
    ]  # fmt: skip


def totals_rows(totals: TokenCostTotals) -> list[dict[str, str]]:
    """The cost tab: token and cost totals of the Run (DD-012 source noted)."""
    cost = fmt_cost(totals.cost_usd, totals.includes_unpriced)
    rows = [
        ("Input tokens", fmt_int(totals.input_tokens)),
        ("Cached input tokens", fmt_int(totals.cached_input_tokens)),
        ("Output tokens", fmt_int(totals.output_tokens)),
        ("Total tokens", fmt_int(totals.total_tokens)),
        ("Cost", cost),
        ("Unpriced LLM calls", fmt_int(totals.unpriced_calls)),
        ("Usage from", "agent span" if totals.usage_source == "agent" else "LLM spans"),
    ]
    return [{"label": k, "value": v} for k, v in rows]


def needs_reload(notice: ChangeNotice, project_id: str, trace_id: str) -> bool:
    """Live append: reload the loaded spans when the notice names this Run (or may, when it
    was truncated)."""
    if not {"spans_committed", "runs_finalized", "purged"} & set(notice.kinds):
        return False
    if notice.truncated:
        return project_id in notice.project_ids or not notice.project_ids
    return any(r.project_id == project_id and r.trace_id == trace_id for r in notice.run_keys)
