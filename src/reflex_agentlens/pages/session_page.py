"""Session detail (plan task P6-08.1; REQ-105.b, 160.b/c): its Runs in order with per-Run and
cumulative tokens and cost, and the cumulative chart."""

from __future__ import annotations

from typing import Any

import reflex as rx
import reflex_silverpoint_react as sp  # type: ignore[import-untyped]

from reflex_agentlens.components.theme import status_badge
from reflex_agentlens.pages._base import AgentlensState, page_frame, ui_event
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.fmt import fmt_cost, fmt_int, fmt_time
from reflex_agentlens.views.registry import registry, services
from reflex_agentlens.views.sessions import SessionRunRow, cumulative_series, session_run_rows

__all__ = ["ON_LOAD", "TITLE", "SessionState", "page"]

TITLE = "Session"


class SessionState(AgentlensState):
    found: bool = False
    external_id: str = ""
    summary: list[dict[str, str]] = []
    rows: list[SessionRunRow] = []
    series: list[dict[str, Any]] = []

    @ui_event
    async def load_session(self, viewer: Viewer) -> None:
        self.found, self.rows, self.series, self.summary = False, [], [], []
        session_id = str(self.router.page.params.get("session_id", ""))
        query, _ = services()
        detail = await query.get_session(viewer, session_id)
        s = detail.session
        self.external_id = s.external_id or s.session_id
        self.summary = [
            {"label": "First activity (UTC)", "value": fmt_time(s.first_ns)},
            {"label": "Last activity (UTC)", "value": fmt_time(s.last_ns)},
            {"label": "Runs", "value": fmt_int(s.run_count)},
            {"label": "Tokens", "value": fmt_int(s.total_tokens)},
            {"label": "Cost", "value": fmt_cost(s.cost_usd, s.includes_unpriced)},
        ]
        self.rows = session_run_rows(detail, registry().prefix)
        self.series = cumulative_series(detail)
        self.found = True


ON_LOAD = [AgentlensState.check_auth, SessionState.load_session]

HEADERS = ("#", "Start (UTC)", "Status", "Name", "Duration", "Tokens", "Cost",
           "Cumulative tokens", "Cumulative cost")  # fmt: skip


def _row(row: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(row.index),
        rx.el.td(rx.el.a(row.start, href=row.href)),
        rx.el.td(status_badge(row.status)),
        rx.el.td(row.name),
        rx.el.td(row.duration),
        rx.el.td(row.tokens),
        rx.el.td(row.cost),
        rx.el.td(row.cumulative_tokens),
        rx.el.td(row.cumulative_cost),
    )


def charts() -> rx.Component:
    """REQ-160.c: cumulative tokens and cost, one point per Run."""
    return rx.cond(
        SessionState.series.length() > 1,  # type: ignore[attr-defined]
        rx.el.div(
            sp.area_chart(data=SessionState.series, x_key="x", value_key="tokens",
                          title="Cumulative tokens"),
            sp.area_chart(data=SessionState.series, x_key="x", value_key="cost",
                          title="Cumulative cost", unit="USD"),
            class_name="agentlens-grid",
        ),
    )  # fmt: skip


def body() -> rx.Component:
    return rx.cond(
        SessionState.found,
        rx.fragment(
            rx.el.h2(SessionState.external_id),
            rx.el.dl(
                rx.foreach(SessionState.summary,
                           lambda i: rx.fragment(rx.el.dt(i["label"]), rx.el.dd(i["value"]))),
                class_name="agentlens-pairs",
            ),
            charts(),
            rx.el.table(
                rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in HEADERS])),
                rx.el.tbody(rx.foreach(SessionState.rows, _row)),
                class_name="agentlens-table",
            ),
        ),
    )  # fmt: skip


def page() -> rx.Component:
    return page_frame(TITLE, body())
