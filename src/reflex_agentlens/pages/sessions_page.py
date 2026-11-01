"""Sessions list (plan task P6-08.1; REQ-105.a, 160.a). UI label: "Sessions (threads)"."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import reflex as rx
from reflex.event import EventSpec

from reflex_agentlens.components.chrome import chrome_bar
from reflex_agentlens.pages._base import AgentlensState, page_frame, ui_event
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.registry import registry, services
from reflex_agentlens.views.sessions import (
    SORTS,
    SessionRow,
    session_rows,
    sessions_href,
    sessions_query,
)

__all__ = ["ON_LOAD", "TITLE", "SessionsState", "page"]

TITLE = "Sessions (threads)"
SORT_LABELS = {
    "last_desc": "Latest activity", "last_asc": "Oldest activity", "runs_desc": "Most runs",
    "runs_asc": "Fewest runs", "tokens_desc": "Most tokens", "tokens_asc": "Fewest tokens",
    "cost_desc": "Most expensive", "cost_asc": "Cheapest",
}  # fmt: skip


class SessionsState(AgentlensState):
    rows: list[SessionRow] = []
    loaded: bool = False
    sort: str = "last_desc"
    next_href: str = ""
    first_href: str = ""
    paged: bool = False

    def _params(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(str(self.router.url)).query)

    @ui_event
    async def load_sessions(self, viewer: Viewer) -> None:
        q = sessions_query(self._params())
        query, _ = services()
        page = await query.list_sessions(viewer, q.filter, q.sort, q.cursor)
        prefix = registry().prefix
        self.rows = session_rows(page.items, prefix)
        self.sort, self.paged, self.loaded = q.sort, q.cursor is not None, True
        self.first_href = sessions_href(prefix, q)
        nxt = page.next_cursor
        self.next_href = "" if nxt is None else sessions_href(prefix, q, cursor=nxt)

    @rx.event
    def pick_sort(self, sort: str) -> EventSpec:
        q = sessions_query(self._params())
        return rx.redirect(
            sessions_href(registry().prefix, q, sort=sort if sort in SORTS else None)
        )


ON_LOAD = [AgentlensState.check_auth, SessionsState.load_sessions]

HEADERS = ("Session", "First activity (UTC)", "Last activity (UTC)", "Span", "Runs", "Tokens",
           "Cost")  # fmt: skip


def _row(row: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(rx.el.a(row.external_id, href=row.href)),
        rx.el.td(row.first),
        rx.el.td(row.last),
        rx.el.td(row.span),
        rx.el.td(row.runs),
        rx.el.td(row.tokens),
        rx.el.td(row.cost),
    )


def body() -> rx.Component:
    return rx.fragment(
        chrome_bar(),
        rx.el.label(
            rx.el.span("Sort"),
            rx.el.select(
                *[rx.el.option(label, value=key) for key, label in SORT_LABELS.items()],
                value=SessionsState.sort,
                on_change=SessionsState.pick_sort,
            ),
        ),
        rx.cond(
            SessionsState.rows.length() > 0,  # type: ignore[attr-defined]
            rx.fragment(
                rx.el.table(
                    rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in HEADERS])),
                    rx.el.tbody(rx.foreach(SessionsState.rows, _row)),
                    class_name="agentlens-table",
                ),
                rx.el.nav(
                    rx.cond(
                        SessionsState.paged, rx.el.a("First page", href=SessionsState.first_href)
                    ),
                    rx.cond(
                        SessionsState.next_href != "",
                        rx.el.a("Next page", href=SessionsState.next_href),
                    ),
                    aria_label="Pagination",
                ),
            ),
            rx.cond(SessionsState.loaded, rx.el.p("No Sessions in this range.")),
        ),
    )


def page() -> rx.Component:
    return page_frame(TITLE, body())
