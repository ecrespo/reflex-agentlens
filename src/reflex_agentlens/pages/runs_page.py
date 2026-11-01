"""Runs page (plan tasks P6-04.1, P6-04.2; REQ-101, 102, 131, 140, 145).

The URL is the source of truth (API §7.2): ``load_runs`` decodes it into a ``RunsQuery``, fills
the filter bar from it and lists one keyset page. Submitting the filter bar, applying a saved
filter or paging navigates to a new URL. While ``live`` is on, a background task follows
``QueryService.subscribe`` (coalesced ≤ 1/s): the live first page refreshes, any other page
only counts "N new runs" (REQ-145.c/d).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import reflex as rx
from reflex.event import EventSpec

from reflex_agentlens.components.chrome import chrome_bar
from reflex_agentlens.components.theme import status_badge
from reflex_agentlens.pages._base import AgentlensState, empty_state, page_frame, ui_event
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.query.filters import RunsQuery, runs_query_string
from reflex_agentlens.views.chrome import decode_chrome
from reflex_agentlens.views.guard import guarded
from reflex_agentlens.views.registry import registry, services
from reflex_agentlens.views.runs import (
    FilterForm,
    RunRow,
    form_from_query,
    is_live_first_page,
    on_notice,
    query_from_form,
    run_key,
    run_rows,
    runs_href,
    runs_query,
)

__all__ = ["ON_LOAD", "TITLE", "RunsState", "page"]

log = logging.getLogger(__name__)

TITLE = "Runs"
STATUSES = ("ok", "error", "in_progress", "incomplete")
SORTS = {
    "start_desc": "Newest first",
    "start_asc": "Oldest first",
    "duration_desc": "Longest",
    "duration_asc": "Shortest",
    "tokens_desc": "Most tokens",
    "tokens_asc": "Fewest tokens",
    "cost_desc": "Most expensive",
    "cost_asc": "Cheapest",
}


def _checked(data: dict[str, Any], key: str) -> bool:
    return data.get(key) in (True, "on", "true", "1")


def form_from_data(data: dict[str, Any]) -> FilterForm:
    """The submitted filter bar → :class:`FilterForm` (unknown keys ignored)."""

    def text(key: str) -> str:
        value = data.get(key, "")
        return value if isinstance(value, str) else ""

    genai = text("genai")
    return FilterForm(
        text=text("q"),
        statuses=[s for s in STATUSES if _checked(data, f"status_{s}")],
        genai=genai if genai in ("1", "0") else "",  # type: ignore[arg-type]
        models=text("models"),
        tools=text("tools"),
        duration_ms=text("dur"),
        tokens=text("tok"),
        cost_usd=text("cost"),
        include_unpriced=_checked(data, "unpriced"),
        attributes=text("attrs"),
        session_id=text("session"),
        sort=text("sort") or "start_desc",
    )


class RunsState(AgentlensState):
    rows: list[RunRow] = []
    loaded: bool = False
    query: str = ""  # canonical query of the page on screen (no cursor)
    next_href: str = ""
    first_href: str = ""
    on_first_page: bool = True
    live_first: bool = False
    new_runs: int = 0
    generation: int = 0
    # filter bar, decoded from the URL
    f_text: str = ""
    f_statuses: list[str] = []
    f_genai: str = ""
    f_models: str = ""
    f_tools: str = ""
    f_dur: str = ""
    f_tok: str = ""
    f_cost: str = ""
    f_unpriced: bool = False
    f_attrs: str = ""
    f_session: str = ""
    f_sort: str = "start_desc"
    # saved filters of the selected Project (REQ-102.a)
    project_id: str = ""
    saved: list[dict[str, str]] = []

    def _url_query(self) -> str:
        return urlsplit(str(self.router.url)).query

    def _set_form(self, q: RunsQuery) -> None:
        form = form_from_query(q)
        self.f_text, self.f_statuses, self.f_genai = form.text, form.statuses, form.genai
        self.f_models, self.f_tools = form.models, form.tools
        self.f_dur, self.f_tok, self.f_cost = form.duration_ms, form.tokens, form.cost_usd
        self.f_unpriced, self.f_attrs = form.include_unpriced, form.attributes
        self.f_session, self.f_sort = form.session_id, form.sort

    async def _list(self, viewer: Viewer, q: RunsQuery) -> None:
        query, _ = services()
        page = await query.list_runs(viewer, q.filter, q.sort, q.cursor)
        prefix = registry().prefix
        self.rows = run_rows(page.items, prefix)
        self.loaded = True
        self.query = runs_query_string(q.filter, q.sort)
        self.first_href = runs_href(prefix, self.query)
        nxt = page.next_cursor
        self.next_href = (
            "" if nxt is None else runs_href(prefix, runs_query_string(q.filter, q.sort, nxt))
        )
        self.on_first_page = q.cursor is None

    async def _load_saved(self, viewer: Viewer, q: RunsQuery) -> None:
        self.project_id, self.saved = "", []
        if len(q.filter.project_ids) != 1:
            return  # saved filters belong to one Project
        query, admin = services()
        key = q.filter.project_ids[0]
        by_key: dict[str, str] = {}
        for p in await query.list_projects(viewer):
            by_key |= {p.id: p.id, p.slug: p.id}
        project_id = by_key.get(key)
        if project_id is None:
            return
        self.project_id = project_id
        self.saved = [
            {"id": f.id, "name": f.name, "href": runs_href(registry().prefix, f.query)}
            for f in await admin.list_saved_filters(viewer, project_id)
        ]

    @ui_event
    async def load_runs(self, viewer: Viewer) -> EventSpec | None:
        """Page ``on_load`` after ``check_auth``: decode the URL, list one page, go live."""
        params = self._url_query()
        q = runs_query(params)
        self._set_form(q)
        self.new_runs = 0
        await self._list(viewer, q)
        await self._load_saved(viewer, q)
        live = decode_chrome(parse_qs(params)).live
        self.live_first = is_live_first_page(q, live)
        self.generation += 1
        return RunsState.watch if live else None

    @ui_event
    async def apply_filters(self, viewer: Viewer, data: dict[str, Any]) -> EventSpec:
        chrome = decode_chrome(parse_qs(self._url_query()))
        query = query_from_form(form_from_data(data), chrome)  # ValidationError names the field
        return rx.redirect(runs_href(registry().prefix, query))

    @rx.event
    def clear_filters(self) -> EventSpec:
        chrome = decode_chrome(parse_qs(self._url_query()))
        return rx.redirect(runs_href(registry().prefix, query_from_form(FilterForm(), chrome)))

    @ui_event
    async def save_filter(self, viewer: Viewer, data: dict[str, Any]) -> None:
        _, admin = services()
        name = data.get("name", "")
        await admin.save_filter(viewer, self.project_id, name if isinstance(name, str) else "",
                                self.query)  # fmt: skip
        await self._load_saved(viewer, runs_query(self._url_query()))

    @ui_event
    async def delete_filter(self, viewer: Viewer, filter_id: str) -> None:
        _, admin = services()
        await admin.delete_filter(viewer, self.project_id, filter_id)
        await self._load_saved(viewer, runs_query(self._url_query()))

    @rx.event(background=True)
    async def watch(self) -> None:
        """Live updates (REQ-140): one subscription per page load; it ends when the page is
        reloaded or left (a newer ``generation`` or another route)."""
        async with self:
            generation = self.generation
            viewer, error = await guarded(lambda: self._viewer(page_load=False))
            q = runs_query(self._url_query())
        if viewer is None or error is not None:
            return
        query, _ = services()
        try:
            async for notice in query.subscribe(viewer, q.filter.project_ids):
                async with self:
                    path = str(self.router.page.path).rstrip("/")
                    if self.generation != generation or not path.endswith("/runs"):
                        return
                    viewer = await self._live_viewer()  # never a stale identity (A-12)
                    if viewer is None:
                        return
                    shown = [run_key(r) for r in self.rows]
                    action = on_notice(notice, shown, live_first_page=self.live_first)
                    if action.kind == "count":
                        self.new_runs += action.new_runs
                    elif action.kind == "refresh":
                        _, error = await guarded(lambda: self._list(viewer, q))  # noqa: B023
                        if error is not None:
                            self._fail(error)
        except Exception as exc:  # the subscription is best effort; the page still works
            log.warning("runs live updates stopped (%s)", type(exc).__name__)


ON_LOAD = [AgentlensState.check_auth, RunsState.load_runs]

# ── components ───────────────────────────────────────────────────────────────────────────────


def _input(name: str, label: str, value: Any, placeholder: str = "") -> rx.Component:
    return rx.el.label(
        rx.el.span(label),
        rx.el.input(name=name, default_value=value, placeholder=placeholder, type="text"),
        class_name="agentlens-field",
    )


def _status_box(status: str) -> rx.Component:
    return rx.el.label(
        rx.el.input(
            type="checkbox",
            name=f"status_{status}",
            default_checked=RunsState.f_statuses.contains(status),  # type: ignore[attr-defined]
        ),
        status_badge(status),
    )


def filter_bar() -> rx.Component:
    """Every REQ-101 filter except Project, Source and time (those are in the chrome)."""
    return rx.el.form(
        _input("q", "Search", RunsState.f_text, "text in names and messages"),
        rx.el.fieldset(
            rx.el.legend("Status"), *[_status_box(s) for s in STATUSES], class_name="agentlens-row"
        ),
        rx.el.label(
            rx.el.span("Type"),
            rx.el.select(
                rx.el.option("Any", value=""),
                rx.el.option("GenAI", value="1"),
                rx.el.option("Generic", value="0"),
                name="genai",
                default_value=RunsState.f_genai,
            ),
        ),
        _input("models", "Models", RunsState.f_models, "gpt-4o, claude"),
        _input("tools", "Tools", RunsState.f_tools, "search"),
        _input("dur", "Duration (ms)", RunsState.f_dur, "100..5000"),
        _input("tok", "Tokens", RunsState.f_tok, "..900"),
        _input("cost", "Cost (USD)", RunsState.f_cost, "0.01.."),
        rx.el.label(
            rx.el.input(type="checkbox", name="unpriced", default_checked=RunsState.f_unpriced),
            rx.el.span("Include unpriced"),
        ),
        rx.el.label(
            rx.el.span("Attributes (one per line)"),
            rx.el.textarea(
                name="attrs", default_value=RunsState.f_attrs, placeholder="root:user.id=42"
            ),
            class_name="agentlens-field",
        ),
        _input("session", "Session", RunsState.f_session),
        rx.el.label(
            rx.el.span("Sort"),
            rx.el.select(
                *[rx.el.option(label, value=key) for key, label in SORTS.items()],
                name="sort",
                default_value=RunsState.f_sort,
            ),
        ),
        rx.el.button("Apply", type="submit"),
        rx.el.button("Clear", type="button", on_click=RunsState.clear_filters),
        on_submit=RunsState.apply_filters,
        reset_on_submit=False,
        key=RunsState.query,  # re-mount with the URL's values after navigation
        class_name="agentlens-filters",
    )


def saved_filters() -> rx.Component:
    return rx.cond(
        RunsState.project_id != "",
        rx.el.section(
            rx.el.h2("Saved filters"),
            rx.el.ul(
                rx.foreach(
                    RunsState.saved,
                    lambda f: rx.el.li(
                        rx.el.a(f["name"], href=f["href"]),
                        rx.el.button(
                            "Delete",
                            type="button",
                            aria_label="Delete saved filter",
                            on_click=RunsState.delete_filter(f["id"]),
                        ),
                    ),
                )
            ),
            rx.el.form(
                rx.el.input(name="name", placeholder="Name this filter", max_length=100),
                rx.el.button("Save current filter", type="submit"),
                on_submit=RunsState.save_filter,
            ),
            class_name="agentlens-saved",
        ),
    )


def _row(row: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(rx.el.a(row.start, href=row.href)),
        rx.el.td(status_badge(row.status)),
        rx.el.td(
            row.name,
            rx.cond(row.generic, rx.el.span(" generic", class_name="agentlens-generic")),
        ),
        rx.el.td(row.source),
        rx.el.td(row.duration),
        rx.el.td(row.models),
        rx.el.td(
            row.tokens,
            rx.cond(row.tokens_note != "", rx.el.sup("*", title=row.tokens_note)),
        ),
        rx.el.td(row.cost),
        rx.el.td(row.session),
    )


HEADERS = ("Start (UTC)", "Status", "Name", "Source", "Duration", "Models", "Tokens", "Cost",
           "Session")  # fmt: skip


def runs_table() -> rx.Component:
    return rx.el.table(
        rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in HEADERS])),
        rx.el.tbody(rx.foreach(RunsState.rows, _row)),
        class_name="agentlens-table",
    )


def pager() -> rx.Component:
    return rx.el.nav(
        rx.cond(~RunsState.on_first_page, rx.el.a("First page", href=RunsState.first_href)),
        rx.cond(RunsState.next_href != "", rx.el.a("Next page", href=RunsState.next_href)),
        aria_label="Pagination",
        class_name="agentlens-pager",
    )


def new_runs_notice() -> rx.Component:
    """REQ-145.d: rows never shift off the live first page; new Runs are only counted."""
    return rx.cond(
        RunsState.new_runs > 0,
        rx.el.a(
            RunsState.new_runs.to_string() + " new runs",  # type: ignore[attr-defined]
            href=RunsState.first_href,
            role="status",
            class_name="agentlens-new-runs",
        ),
    )


def body() -> rx.Component:
    return rx.fragment(
        chrome_bar(),
        filter_bar(),
        saved_filters(),
        new_runs_notice(),
        rx.cond(
            RunsState.rows.length() > 0,  # type: ignore[attr-defined]
            rx.fragment(runs_table(), pager()),
            rx.cond(
                RunsState.loaded & (RunsState.query == ""),
                empty_state(),
                rx.cond(RunsState.loaded, rx.el.p("No Runs match these filters.")),
            ),
        ),
    )


def page() -> rx.Component:
    return page_frame(TITLE, body())
