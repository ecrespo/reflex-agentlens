"""Trace page (plan tasks P6-05.2, P6-06.2, P6-07.2; REQ-037.b, 103.b, 131, 150-154, API §7.2).

``/trace/[trace_id]?project=&tab=&span=&replay=&speed=``: ``load_trace`` resolves the trace ID
among the viewer's Projects (several matches → disambiguation links, none → not found), loads
the first page of the span tree and renders the waterfall. Spans past the page size load on
demand; while the Run is ``in_progress`` a background task reloads the loaded spans on each
change notice that names it.

Selecting a span (waterfall row or graph node) opens the drawer; the raw tab is built only when
opened. Tab, selection and replay changes rewrite the URL in place (``history.replaceState``),
so a link reproduces the view without reloading the trace on every click. Replay (REQ-154)
steps the spans in start order at speed 1, 2, 5 or by hand.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import reflex as rx
from reflex.event import EventSpec
from reflex_mermaidjs import mermaid

from reflex_agentlens.components.drawer import drawer
from reflex_agentlens.components.theme import kind_badge, status_badge
from reflex_agentlens.core.errors import NotFound
from reflex_agentlens.pages._base import AgentlensState, page_frame, ui_event
from reflex_agentlens.query.dto import AgentGraph, RunRef, RunSummary, SpanNode, SpanRaw, Viewer
from reflex_agentlens.views.detail import DrawerView, Section, drawer_view, raw_sections
from reflex_agentlens.views.fmt import fmt_duration, fmt_time
from reflex_agentlens.views.graph import graph_text, replay_delay_ms, replay_order, replay_step
from reflex_agentlens.views.guard import guarded
from reflex_agentlens.views.registry import registry, services
from reflex_agentlens.views.trace import (
    SPEEDS,
    TraceUrl,
    WaterfallRow,
    candidate_links,
    decode_trace_url,
    is_hex_id,
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
SPEED_LABELS = {"1": "1x", "2": "2x", "5": "5x", "step": "Step"}


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
    # span drawer (P6-06.2)
    drawer: DrawerView | None = None
    drawer_tab: str = "detail"
    raw_sections: list[Section] = []
    # graph and replay (P6-07.2)
    chart: str = ""
    graph_note: str = ""
    replay: str = ""
    speed: str = "1"
    playing: bool = False
    future: list[str] = []
    # backend only: the loaded tree, re-rendered on every toggle
    _run: RunSummary | None = None
    _spans: list[SpanNode] = []
    _next_offset: int | None = None
    _ref: RunRef | None = None
    _graph: AgentGraph | None = None
    _node_spans: dict[str, list[str]] = {}
    _raw: SpanRaw | None = None
    _trace_id: str = ""
    _project: str | None = None

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

    def _draw_graph(self) -> None:
        if self._graph is None:
            self.chart, self.graph_note, self._node_spans = "", "", {}
            return
        text = graph_text(self._graph, self.replay or self.selected or None)
        self.chart, self.graph_note, self._node_spans = text.text, text.note, text.node_spans

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
        self._draw_graph()

    def _reset(self) -> None:
        self.found, self.candidates, self.rows, self.totals, self.raw = False, [], [], [], []
        self.heading = self.run_status = self.started = self.duration = ""
        self.in_progress = self.has_more = self.playing = False
        self.expanded, self.collapsed, self.future, self.raw_sections = [], [], [], []
        self.drawer, self.drawer_tab, self.chart, self.graph_note = None, "detail", "", ""
        self._run, self._spans, self._next_offset, self._ref = None, [], None, None
        self._graph, self._node_spans, self._raw = None, {}, None

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
        self._graph = detail.graph
        self._show(detail.run, spans, nxt)

    async def _open_drawer(self, viewer: Viewer, span_id: str) -> None:
        self.drawer, self._raw, self.raw_sections, self.drawer_tab = None, None, [], "detail"
        if self._ref is None or not is_hex_id(span_id, 16):
            return
        query, _ = services()
        try:
            detail = await query.get_span_detail(viewer, self._ref, span_id)
        except NotFound:
            return  # a stale ``span=`` link: the trace still shows
        self.drawer, self._raw = drawer_view(detail), detail.raw

    def _sync_url(self) -> EventSpec:
        """Rewrite the address bar for the current view (no navigation, no reload)."""
        url = TraceUrl(
            tab=self.tab,  # type: ignore[arg-type]
            span=self.selected or None,
            replay=self.replay or None,
            speed=self.speed,
            project=self._project,
        )
        href = trace_page_href(registry().prefix, self._trace_id, url)
        return rx.call_script(f"window.history.replaceState(null, '', {json.dumps(href)})")

    @ui_event
    async def load_trace(self, viewer: Viewer) -> EventSpec | None:
        """Page ``on_load`` after ``check_auth``."""
        self._reset()
        self.generation += 1
        trace_id, url = self._url()
        self._trace_id, self._project = trace_id, url.project
        self.tab, self.selected = url.tab, url.span or ""
        self.replay, self.speed = url.replay or "", url.speed
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
        if self.selected:
            await self._open_drawer(viewer, self.selected)
        if self.replay:
            self._replay_to(self.replay)
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

    @rx.event
    def pick_tab(self, tab: str) -> EventSpec:
        picked = decode_trace_url({"tab": tab}).tab
        self.tab = picked if picked in self.tabs else "waterfall"
        return self._sync_url()

    @ui_event
    async def select_span(self, viewer: Viewer, span_id: str) -> EventSpec | None:
        if not is_hex_id(span_id, 16):
            return None
        self.selected = span_id
        await self._open_drawer(viewer, span_id)
        self._draw_graph()
        return self._sync_url()

    @rx.event
    def close_drawer(self) -> EventSpec:
        self.selected, self.drawer, self._raw, self.raw_sections = "", None, None, []
        self._draw_graph()
        return self._sync_url()

    @rx.event
    def pick_drawer_tab(self, tab: str) -> None:
        self.drawer_tab = "raw" if tab == "raw" else "detail"
        if self.drawer_tab == "raw" and self._raw is not None and not self.raw_sections:
            self.raw_sections = raw_sections(self._raw)  # built only when opened

    @rx.event
    def on_node_click(self, event: dict[str, Any]) -> Any:
        """A graph node selects its first span (REQ-153)."""
        spans = self._node_spans.get(str(event.get("node_id", "")))
        if not spans:
            return None
        return TraceState.select_span(spans[0])

    # ── replay (REQ-154) ─────────────────────────────────────────────────────────────────────

    def _replay_to(self, span_id: str | None) -> None:
        order = replay_order(self._spans)
        if span_id not in order:
            span_id = replay_step(order, None, 1)
        self.replay = span_id or ""
        self.future = order[order.index(span_id) + 1 :] if span_id else []
        self._draw_graph()

    @rx.event
    def start_replay(self) -> EventSpec:
        self._replay_to(None)
        return self._sync_url()

    @rx.event
    def step_replay(self, delta: int) -> EventSpec:
        self.playing = False
        self._replay_to(replay_step(replay_order(self._spans), self.replay or None, delta))
        return self._sync_url()

    @rx.event
    def set_speed(self, speed: str) -> EventSpec:
        self.speed = speed if speed in SPEEDS else "1"
        if self.speed == "step":
            self.playing = False
        return self._sync_url()

    @rx.event
    def exit_replay(self) -> EventSpec:
        self.replay, self.future, self.playing = "", [], False
        self._draw_graph()
        return self._sync_url()

    @rx.event
    def toggle_play(self) -> EventSpec | None:
        if self.playing or self.speed == "step" or not self.replay:
            self.playing = False
            return None
        self.playing = True
        return TraceState.play

    def _playing(self, generation: int) -> bool:
        return self.playing and self.generation == generation

    @rx.event(background=True)
    async def play(self) -> None:
        """Advance one span per delay until the end, a pause, or a reload."""
        async with self:
            generation = self.generation
        while True:
            async with self:
                if not self._playing(generation):
                    return
                order = replay_order(self._spans)
                nxt = replay_step(order, self.replay or None, 1)
                if nxt is None or nxt == self.replay:
                    self.playing = False
                    return
                starts = {s.span_id: s.start_ns for s in self._spans}
                gap = starts.get(nxt, 0) - starts.get(self.replay, 0)
                delay = replay_delay_ms(self.speed, gap)
                if delay is None:
                    self.playing = False
                    return
            await asyncio.sleep(delay / 1000)
            async with self:
                if not self._playing(generation):
                    return
                self._replay_to(nxt)

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
                    viewer = await self._live_viewer()  # never a stale identity (A-12)
                    if viewer is None:
                        return
                    _, error = await guarded(lambda: self._reload(viewer, ref))  # noqa: B023
                    if error is not None:
                        self._fail(error)
                        return
        except Exception as exc:  # live append is best effort; the page still works
            log.warning("trace live updates stopped (%s)", type(exc).__name__)


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
                    on_click=TraceState.select_span(row.span_id),
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
        aria_selected=(row.span_id == TraceState.selected) | (row.span_id == TraceState.replay),
        class_name=rx.cond(
            (row.span_id == TraceState.selected) | (row.span_id == TraceState.replay),
            "agentlens-selected",
            rx.cond(TraceState.future.contains(row.span_id), "agentlens-future", ""),  # type: ignore[attr-defined]
        ),
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
        ("graph", graph_panel()),
        waterfall(),
    )


def graph_panel() -> rx.Component:
    """REQ-153: the agent graph; a node click selects its span. ``strict`` Mermaid, no icon
    packs, no Font Awesome (REQ-324)."""
    return rx.cond(
        TraceState.chart != "",
        rx.fragment(
            rx.cond(TraceState.graph_note != "", rx.el.p(TraceState.graph_note, role="note")),
            mermaid(
                TraceState.chart,
                security_level="strict",
                font_awesome=False,
                icon_packs=[],
                html_labels=False,
                deterministic_ids=True,
                node_click=True,
                on_node_click=TraceState.on_node_click,
                pan_zoom=True,
            ),
        ),
        rx.el.p("No agent graph was recorded for this Run."),
    )


def replay_bar() -> rx.Component:
    """REQ-154: replay the Run span by span."""
    return rx.cond(
        TraceState.replay == "",
        rx.el.button("Replay", type="button", on_click=TraceState.start_replay),
        rx.el.div(
            rx.el.button(
                "◀",
                type="button",
                aria_label="Previous span",
                on_click=TraceState.step_replay(-1),  # type: ignore[operator]
            ),
            rx.el.button(
                rx.cond(TraceState.playing, "Pause", "Play"),
                type="button",
                on_click=TraceState.toggle_play,
                disabled=TraceState.speed == "step",
            ),
            rx.el.button(
                "▶",
                type="button",
                aria_label="Next span",
                on_click=TraceState.step_replay(1),  # type: ignore[operator]
            ),
            rx.el.select(
                *[rx.el.option(label, value=key) for key, label in SPEED_LABELS.items()],
                value=TraceState.speed,
                on_change=TraceState.set_speed,
                aria_label="Replay speed",
            ),
            rx.el.button("Exit replay", type="button", on_click=TraceState.exit_replay),
            role="toolbar",
            aria_label="Replay",
            class_name="agentlens-replay",
        ),
    )


def span_drawer() -> rx.Component:
    return rx.cond(
        TraceState.drawer,
        drawer(
            TraceState.drawer,
            TraceState.drawer_tab,
            TraceState.raw_sections,
            TraceState.pick_drawer_tab,
            TraceState.close_drawer,
        ),
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
        rx.fragment(header(), replay_bar(), tab_bar(), tab_body(), span_drawer()),
        rx.cond(TraceState.candidates.length() > 0, disambiguation()),  # type: ignore[attr-defined]
    )


def page() -> rx.Component:
    return page_frame(TITLE, body())
