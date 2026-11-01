"""Dashboard (plan task P6-09.1; REQ-106, 107, 130.c/d, 141, DD-017).

KPI row, then a grid of silverpoint charts, each with its own empty and error state
(``DashboardData.failed``). Clicking a model, tool, Source or time bucket opens Runs with that
filter (REQ-141.c); the URL is looked up on the server from the clicked row's ``link`` key.
While live mode is on, aggregates refresh at most every 10 s when new data arrives (REQ-141.d).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import reflex as rx
import reflex_silverpoint_react as sp  # type: ignore[import-untyped]
from reflex.event import EventSpec

from reflex_agentlens.components.chrome import chrome_bar
from reflex_agentlens.pages._base import AgentlensState, empty_state, page_frame, ui_event
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.chrome import Chrome, decode_chrome
from reflex_agentlens.views.dashboard import Links, agg_scope, dashboard_charts, kpi_cards
from reflex_agentlens.views.guard import guarded
from reflex_agentlens.views.registry import registry, services

__all__ = ["LIVE_INTERVAL_S", "ON_LOAD", "TITLE", "DashboardState", "page"]

log = logging.getLogger(__name__)

TITLE = "Dashboard"
LIVE_INTERVAL_S = 10.0  # REQ-141.d
Rows = list[dict[str, Any]]


class DashboardState(AgentlensState):
    loaded: bool = False
    has_data: bool = False
    generation: int = 0
    kpis: list[dict[str, str]] = []
    failed: list[str] = []
    runs_by_status: Rows = []
    status_keys: list[str] = []
    tokens_by_model: Rows = []
    cost_by_model: Rows = []
    tokens_over_time: Rows = []
    model_keys: list[str] = []
    latency_by_operation: Rows = []
    latency_by_node: Rows = []
    tool_outcomes: Rows = []
    top_tools: Rows = []
    cost_by_source: Rows = []
    source_donut: bool = True
    approvals: Rows = []
    approval_wait: str = ""
    metric_tokens: Rows = []
    metric_keys: list[str] = []
    _links: dict[str, str] = {}

    def _chrome(self) -> Chrome:
        return decode_chrome(parse_qs(urlsplit(str(self.router.url)).query))

    async def _load(self, viewer: Viewer, chrome: Chrome) -> None:
        query, _ = services()
        data = await query.dashboard(viewer, agg_scope(chrome))
        charts, links = dashboard_charts(data, registry().prefix, chrome)
        self.kpis = kpi_cards(data.kpis)
        self.has_data = bool(data.kpis and data.kpis.runs)
        for name, value in vars(charts).items():
            setattr(self, name, value)
        self._links = links.urls
        self.loaded = True

    @ui_event
    async def load_dashboard(self, viewer: Viewer) -> EventSpec | None:
        chrome = self._chrome()
        await self._load(viewer, chrome)
        self.generation += 1
        return DashboardState.watch if chrome.live else None

    @rx.event
    def select(self, chart: str, item: dict[str, Any]) -> EventSpec | None:
        """REQ-141.c: a clicked chart element → Runs with its filter."""
        url = Links(registry().prefix, Chrome(), dict(self._links)).resolve(chart, item)
        return rx.redirect(url) if url else None

    @rx.event(background=True)
    async def watch(self) -> None:
        """Live refresh ≤ 1 per 10 s (REQ-141.d); ends on reload or when the page is left."""
        async with self:
            generation, chrome = self.generation, self._chrome()
            viewer, error = await guarded(lambda: self._viewer(page_load=False))
        if viewer is None or error is not None:
            return
        query, _ = services()
        ids = [chrome.project] if chrome.project else []
        try:
            async for _notice in query.subscribe(viewer, ids, min_interval=LIVE_INTERVAL_S):
                async with self:
                    path = str(self.router.page.path).rstrip("/")
                    if self.generation != generation or path != registry().prefix.rstrip("/"):
                        return
                    viewer = await self._live_viewer()  # never a stale identity (A-12)
                    if viewer is None:
                        return
                    _, error = await guarded(lambda: self._load(viewer, chrome))  # noqa: B023
                    if error is not None:
                        self._fail(error)
        except Exception as exc:  # live refresh is best effort; the page still works
            log.warning("dashboard live refresh stopped (%s)", type(exc).__name__)


ON_LOAD = [AgentlensState.check_auth, DashboardState.load_dashboard]

# ── components ───────────────────────────────────────────────────────────────────────────────


def _on_select(chart: str) -> Any:
    return lambda item: DashboardState.select(chart, item)  # type: ignore[operator]


def panel(name: str, title: str, rows: Any, chart: rx.Component) -> rx.Component:
    """One chart with its own error (failed aggregate) and empty states (REQ-130.d)."""
    return rx.el.section(
        rx.cond(
            DashboardState.failed.contains(name),  # type: ignore[attr-defined]
            rx.el.div(rx.el.h3(title), rx.el.p("This panel could not be loaded."), role="alert",
                      class_name="agentlens-panel-error"),
            rx.cond(rows.length() > 0, chart,
                    rx.el.div(rx.el.h3(title), rx.el.p("No data in this range."))),
        ),
        class_name="agentlens-panel",
    )  # fmt: skip


def kpi_row() -> rx.Component:
    return rx.el.div(
        rx.foreach(
            DashboardState.kpis,
            lambda k: sp.kpi_card(data=[], title=k["title"], value=k["value"],
                                  footer_left=k["footer"], height=60),
        ),
        rx.cond(DashboardState.failed.contains("kpis"),  # type: ignore[attr-defined]
                rx.el.p("KPIs could not be loaded.", role="alert")),
        class_name="agentlens-kpis",
    )  # fmt: skip


def charts() -> rx.Component:
    s = DashboardState
    return rx.el.div(
        panel("runs_over_time", "Runs by status", s.runs_by_status,
              sp.stacked_bar_chart(data=s.runs_by_status, x_key="x", keys=s.status_keys,
                                   title="Runs by status", on_select=_on_select("runs"))),
        panel("model_usage", "Tokens by model", s.tokens_by_model,
              sp.stacked_bar_chart(data=s.tokens_by_model, x_key="x",
                                   keys=["input", "output", "cached"],
                                   names=["Input", "Output", "Cached input"],
                                   title="Tokens by model", on_select=_on_select("model"))),
        panel("model_usage", "Cost by model", s.cost_by_model,
              sp.bar_chart(data=s.cost_by_model, x_key="x", value_key="value",
                           orientation="rows", title="Cost by model", unit="USD",
                           on_select=_on_select("model"))),
        panel("model_usage", "Tokens over time by model", s.tokens_over_time,
              sp.stacked_bar_chart(data=s.tokens_over_time, x_key="x", keys=s.model_keys,
                                   title="Tokens over time by model")),
        panel("latency_by_operation", "Latency by operation", s.latency_by_operation,
              sp.dumbbell_chart(data=s.latency_by_operation, label_key="label",
                                start_key="p50", end_key="p95", names=["p50", "p95"],
                                title="Latency by operation", unit="ms")),
        panel("latency_by_node", "Latency by graph node", s.latency_by_node,
              sp.dumbbell_chart(data=s.latency_by_node, label_key="label", start_key="p50",
                                end_key="p95", names=["p50", "p95"],
                                title="Latency by graph node", unit="ms")),
        panel("tools", "Tool success vs. error", s.tool_outcomes,
              sp.stacked_bar_chart(data=s.tool_outcomes, x_key="x", keys=["success", "error"],
                                   names=["Success", "Error"], title="Tool success vs. error",
                                   on_select=_on_select("tool"))),
        panel("tools", "Top tools", s.top_tools,
              sp.bar_chart(data=s.top_tools, x_key="x", value_key="value", orientation="rows",
                           title="Top tools", on_select=_on_select("tool"))),
        panel("by_source", "Cost by Source", s.cost_by_source,
              rx.cond(s.source_donut,
                      sp.donut_chart(data=s.cost_by_source, name_key="name", value_key="value",
                                     title="Cost by Source", on_select=_on_select("source")),
                      sp.bar_chart(data=s.cost_by_source, x_key="x", value_key="value",
                                   orientation="rows", title="Cost by Source"))),
        panel("approvals", "Approval outcomes", s.approvals,
              sp.donut_chart(data=s.approvals, name_key="name", value_key="value",
                             title="Approval outcomes", footer_left=s.approval_wait)),
        rx.cond(s.metric_tokens.length() > 0,  # type: ignore[attr-defined]  # REQ-141.e
                panel("metric_tokens", "Token usage (metrics)", s.metric_tokens,
                      sp.stacked_bar_chart(data=s.metric_tokens, x_key="x", keys=s.metric_keys,
                                           title="Token usage (metrics)"))),
        class_name="agentlens-grid",
    )  # fmt: skip


def body() -> rx.Component:
    return rx.fragment(
        chrome_bar(),
        rx.cond(
            DashboardState.has_data | (DashboardState.failed.length() > 0),  # type: ignore[attr-defined]
            rx.fragment(kpi_row(), charts()),
            rx.cond(DashboardState.loaded, empty_state()),
        ),
    )


def page() -> rx.Component:
    return page_frame(TITLE, body())
