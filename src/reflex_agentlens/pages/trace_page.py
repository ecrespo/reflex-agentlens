"""Trace page (plan tasks P6-05.1, P6-05.2; REQ-037.b, 103.b, 131, 150, API §7.2).

``/trace/[trace_id]?project=&tab=&span=&replay=&speed=``: ``load_trace`` resolves the trace ID
among the viewer's Projects (several matches → disambiguation links, none → not found), loads
the first page of the span tree and renders the waterfall. Spans past the page size load on
demand; while the Run is ``in_progress`` a background task reloads the loaded spans on each
change notice that names it. The graph tab and the span drawer arrive with P6-07.2 / P6-06.2.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import reflex as rx
from reflex.event import EventSpec

from reflex_agentlens.components.theme import kind_badge, status_badge
from reflex_agentlens.pages._base import AgentlensState, page_frame, ui_event
from reflex_agentlens.query.dto import RunRef, RunSummary, SpanNode, Viewer
from reflex_agentlens.views.fmt import fmt_duration, fmt_time
from reflex_agentlens.views.guard import guarded
from reflex_agentlens.views.registry import registry, services
from reflex_agentlens.views.trace import (
    TraceUrl,
    WaterfallRow,
    candidate_links,
    decode_trace_url,
    needs_reload,
    tabs_for,
    toggle,
    totals_rows,
    trace_page_href,
    waterfall_rows,
)

__all__ = ["ON_LOAD", "TITLE", "TraceState", "page"]

log = logging.getLogger(__name__)

TITLE = "Trace"
TAB_LABELS = {"waterfall": "Waterfall", "graph": "Graph", "cost": "Cost", "raw": "Raw"}


class TraceState(AgentlensState):
    found: bool = False
    candidates: list[dict[str, str]] = []
    heading: str = ""
    run_status: str = ""
    started: str = ""
    duration: str = ""
    in_progress: bool = False
    tabs: list[str] = []
    tab: str = "waterfall"
    selected: str = ""
    rows: list[WaterfallRow] = []
    totals: list[dict[str, str]] = []
    raw: list[dict[str, str]] = []
    has_more: bool = False
    generation: int = 0
    expanded: list[str] = []
    collapsed: list[str] = []
    # backend only: the loaded tree, re-rendered on every toggle
    _run: RunSummary | None = None
    _spans: list[SpanNode] = []
    _next_offset: int | None = None
    _ref: RunRef | None = None

    def _url(self) -> tuple[str, TraceUrl]:
        trace_id = str(self.router.page.params.get("trace_id", ""))
        return trace_id, decode_trace_url(parse_qs(urlsplit(str(self.router.url)).query))

    def _render(self) -> None:
        if self._run is None:
            self.rows = []
            return
        self.rows = waterfall_rows(
            self._run,
            self._spans,
            expanded=frozenset(self.expanded),
            collapsed=frozenset(self.collapsed),
        )

    def _show(self, run: RunSummary, spans: list[SpanNode], next_offset: int | None) -> None:
        self._run, self._spans, self._next_offset = run, spans, next_offset
        self.found = True
        self.heading = run.agent_name or run.root_name or run.trace_id
        self.run_status = run.status
        self.started = fmt_time(run.start_ns)
        self.duration = fmt_duration(run.duration_ns)
        self.in_progress = run.status == "in_progress"
        self.tabs = tabs_for(run)
        self.has_more = next_offset is not None
        self.raw = [
            {"label": k, "value": v}
            for k, v in (
                ("Project", run.project_id),
                ("Trace ID", run.trace_id),
                ("Source", run.source.label if run.source else "—"),
                ("Spans", str(run.span_count)),
                ("Missing parents", str(run.missing_parents)),
                ("Session", run.session_id or "—"),
            )
        ]
        self._render()

    def _reset(self) -> None:
        self.found, self.candidates, self.rows, self.totals, self.raw = False, [], [], [], []
        self.heading = self.run_status = self.started = self.duration = ""
        self.in_progress = self.has_more = False
        self.expanded, self.collapsed = [], []
        self._run, self._spans, self._next_offset, self._ref = None, [], None, None

    async def _reload(self, viewer: Viewer, ref: RunRef) -> None:
        """Every span loaded so far, fresh (live append keeps the same depth of loading)."""
        query, _ = services()
        want = max(len(self._spans), 1)
        detail = await query.get_trace(viewer, ref)
        spans, nxt = list(detail.spans), detail.next_offset
        while nxt is not None and len(spans) < want:
            more = await query.get_trace(viewer, ref, nxt)
            spans += more.spans
            nxt = more.next_offset
        self.totals = totals_rows(detail.totals)
        self._show(detail.run, spans, nxt)

    @ui_event
    async def load_trace(self, viewer: Viewer) -> EventSpec | None:
        """Page ``on_load`` after ``check_auth``."""
        self._reset()
        self.generation += 1
        trace_id, url = self._url()
        self.tab, self.selected = url.tab, url.span or ""
        query, _ = services()
        refs = await query.resolve_trace(viewer, trace_id, url.project)
        if len(refs) > 1:
            self.candidates = candidate_links(refs, registry().prefix, url)
            return None
        if not refs:
            self.error_kind, self.error_message = "not_found", "This trace was not found."
            return None
        self._ref = refs[0]
        await self._reload(viewer, refs[0])
        if self.tab not in self.tabs:
            self.tab = "waterfall"  # REQ-131: a generic trace has no GenAI panels
        return TraceState.follow if self.in_progress else None

    @ui_event
    async def load_more(self, viewer: Viewer) -> None:
        if self._ref is None or self._run is None or self._next_offset is None:
            return
        query, _ = services()
        more = await query.get_trace(viewer, self._ref, self._next_offset)
        self._show(self._run, [*self._spans, *more.spans], more.next_offset)

    @rx.event
    def toggle_row(self, span_id: str, depth: int, currently_collapsed: bool) -> None:
        expanded, collapsed = toggle(
            span_id, depth, frozenset(self.expanded), frozenset(self.collapsed),
            currently_collapsed,
        )  # fmt: skip
        self.expanded, self.collapsed = sorted(expanded), sorted(collapsed)
        self._render()

    def _go(self, url: TraceUrl) -> EventSpec:
        trace_id, _ = self._url()
        return rx.redirect(trace_page_href(registry().prefix, trace_id, url), replace=True)

    @rx.event
    def pick_tab(self, tab: str) -> EventSpec:
        _, url = self._url()
        return self._go(decode_trace_url({**_pairs(url), "tab": tab}))

    @rx.event
    def select_span(self, span_id: str) -> EventSpec:
        _, url = self._url()
        return self._go(decode_trace_url({**_pairs(url), "span": span_id}))

    @rx.event(background=True)
    async def follow(self) -> None:
        """Live append while the Run is in progress (REQ-140, 150)."""
        async with self:
            generation, ref = self.generation, self._ref
            viewer, error = await guarded(lambda: self._viewer(page_load=False))
        if ref is None or viewer is None or error is not None:
            return
        query, _ = services()
        try:
            async for notice in query.subscribe(viewer, [ref.project_id]):
                if not needs_reload(notice, ref.project_id, ref.trace_id):
                    continue
                async with self:
                    if self.generation != generation or not self.in_progress:
                        return
                    _, error = await guarded(lambda: self._reload(viewer, ref))
                    if error is not None:
                        self._fail(error)
                        return
        except Exception as exc:  # live append is best effort; the page still works
            log.warning("trace live updates stopped (%s)", type(exc).__name__)


def _pairs(url: TraceUrl) -> dict[str, str]:
    out = {"tab": url.tab, "speed": url.speed}
    out |= {k: v for k, v in (("span", url.span), ("replay", url.replay),
                              ("project", url.project)) if v}  # fmt: skip
    return out


ON_LOAD = [AgentlensState.check_auth, TraceState.load_trace]

# ── components ───────────────────────────────────────────────────────────────────────────────


def _bar(row: Any) -> rx.Component:
    return rx.el.div(
        rx.el.div(
            style={
                "margin_left": row.offset_pct.to_string() + "%",
                "width": row.width_pct.to_string() + "%",
                "height": "10px",
                "background": rx.cond(row.error, row.status_color, row.color),
                "opacity": rx.cond(row.placeholder, "0.3", "1"),
            }
        ),
        style={"width": "100%", "background": "transparent"},
    )


def _waterfall_row(row: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(
            rx.el.div(
                rx.cond(
                    row.has_children,
                    rx.el.button(
                        rx.cond(row.collapsed, "▸", "▾"),
                        type="button",
                        aria_label=rx.cond(row.collapsed, "Expand", "Collapse"),
                        aria_expanded=~row.collapsed,
                        on_click=TraceState.toggle_row(  # type: ignore[operator]
                            row.span_id, row.depth, row.collapsed
                        ),
                    ),
                ),
                kind_badge(row.kind),
                rx.el.button(
                    rx.cond(row.placeholder, "(missing parent span)", row.label),
                    type="button",
                    on_click=TraceState.select_span(row.span_id),  # type: ignore[operator]
                    class_name="agentlens-link",
                ),
                rx.cond(row.badges != "", rx.el.small(" " + row.badges)),
                style={
                    "padding_left": row.indent_px.to_string() + "px",
                    "display": "flex",
                    "gap": "6px",
                    "align_items": "center",
                },
            ),
        ),
        rx.el.td(_bar(row), style={"width": "40%"}),
        rx.el.td(row.duration),
        rx.el.td(row.tokens),
        rx.el.td(row.cost),
        aria_selected=row.span_id == TraceState.selected,
        class_name=rx.cond(row.span_id == TraceState.selected, "agentlens-selected", ""),
    )


def waterfall() -> rx.Component:
    return rx.el.div(
        rx.el.table(
            rx.el.thead(
                rx.el.tr(
                    *[
                        rx.el.th(h, scope="col")
                        for h in ("Span", "Timeline", "Duration", "Tokens", "Cost")
                    ]
                )
            ),
            rx.el.tbody(rx.foreach(TraceState.rows, _waterfall_row)),
            class_name="agentlens-table agentlens-waterfall",
        ),
        rx.cond(
            TraceState.has_more,
            rx.el.button("Load more spans", type="button", on_click=TraceState.load_more),
        ),
    )


def _pairs_table(items: Any) -> rx.Component:
    return rx.el.dl(
        rx.foreach(items, lambda i: rx.fragment(rx.el.dt(i["label"]), rx.el.dd(i["value"]))),
        class_name="agentlens-pairs",
    )


def tab_bar() -> rx.Component:
    return rx.el.div(
        rx.foreach(
            TraceState.tabs,
            lambda t: rx.el.button(
                rx.Var.create(TAB_LABELS)[t],
                type="button",
                role="tab",
                aria_selected=TraceState.tab == t,
                on_click=TraceState.pick_tab(t),  # type: ignore[operator]
            ),
        ),
        role="tablist",
        class_name="agentlens-tabs",
    )


def tab_body() -> rx.Component:
    return rx.match(
        TraceState.tab,
        ("cost", _pairs_table(TraceState.totals)),
        ("raw", _pairs_table(TraceState.raw)),
        ("graph", rx.el.p("The agent graph is not available yet.")),
        waterfall(),
    )


def header() -> rx.Component:
    return rx.el.header(
        rx.el.h2(TraceState.heading),
        status_badge(TraceState.run_status),
        rx.el.span(TraceState.started + " UTC · " + TraceState.duration),
        rx.cond(TraceState.in_progress, rx.el.span(" live", role="status")),
        class_name="agentlens-trace-header",
    )


def disambiguation() -> rx.Component:
    """REQ-103.b: the trace ID exists in several Projects."""
    return rx.el.section(
        rx.el.h2("This trace ID exists in several Projects"),
        rx.el.ul(
            rx.foreach(
                TraceState.candidates,
                lambda c: rx.el.li(rx.el.a(c["project"], href=c["href"])),
            )
        ),
    )


def body() -> rx.Component:
    return rx.cond(
        TraceState.found,
        rx.fragment(header(), tab_bar(), tab_body()),
        rx.cond(TraceState.candidates.length() > 0, disambiguation()),  # type: ignore[attr-defined]
    )


def page() -> rx.Component:
    return page_frame(TITLE, body())
