"""Shared chrome: Project, Source and time-range selectors (plan task P6-02.1; REQ-130.a).

The selection lives in the URL (API §7.2); changing a selector navigates to the same path with
the new query, so every page reloads its data from one source of truth.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import reflex as rx
from reflex.event import EventSpec

from reflex_agentlens.pages._base import AgentlensState, ui_event
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.chrome import (
    CUSTOM,
    RANGES,
    Chrome,
    decode_chrome,
    encode_chrome,
    project_options,
    source_options,
)
from reflex_agentlens.views.registry import services

__all__ = ["ChromeState", "chrome_bar"]

RANGE_LABELS = {"1h": "1 h", "24h": "24 h", "7d": "7 d", "30d": "30 d", CUSTOM: "Custom"}


class ChromeState(AgentlensState):
    projects: list[dict[str, str]] = []
    sources: list[dict[str, str]] = []
    project: str = ""
    source: str = ""
    range: str = "24h"
    live: bool = True

    def _current(self) -> tuple[str, Chrome]:
        parts = urlsplit(str(self.router.url))
        return parts.path, decode_chrome(parse_qs(parts.query))

    @ui_event
    async def load_chrome(self, viewer: Viewer) -> None:
        query, _ = services()
        _, chrome = self._current()
        projects = project_options(viewer, await query.list_projects(viewer))
        ids = [chrome.project] if chrome.project else []
        sources = source_options(await query.list_sources(viewer, ids))
        self.projects = [{"value": o.value, "label": o.label} for o in projects]
        self.sources = [{"value": o.value, "label": o.label} for o in sources]
        self.project = chrome.project or ""
        self.source = chrome.sources[0] if chrome.sources else ""
        self.range, self.live = chrome.range, chrome.live

    def _go(self, chrome: Chrome) -> EventSpec:
        path, _ = self._current()
        query = urlencode(encode_chrome(chrome))
        return rx.redirect(f"{path}?{query}" if query else path)

    @rx.event
    def pick_project(self, value: str) -> EventSpec:
        _, chrome = self._current()
        return self._go(
            Chrome(value or None, (), chrome.range, chrome.from_, chrome.to, chrome.live)
        )

    @rx.event
    def pick_source(self, value: str) -> EventSpec:
        _, c = self._current()
        return self._go(
            Chrome(c.project, (value,) if value else (), c.range, c.from_, c.to, c.live)
        )

    @rx.event
    def pick_range(self, key: str) -> EventSpec:
        _, chrome = self._current()
        return self._go(chrome.with_range(key))


def _select(value: Any, options: Any, on_change: Any,
            empty: str, label: str) -> rx.Component:  # fmt: skip
    return rx.el.label(
        rx.el.span(label, class_name="agentlens-sr-only"),
        rx.el.select(
            rx.el.option(empty, value=""),
            rx.foreach(options, lambda o: rx.el.option(o["label"], value=o["value"])),
            value=value,
            on_change=on_change,
        ),
    )


def chrome_bar() -> rx.Component:
    return rx.el.nav(
        _select(ChromeState.project, ChromeState.projects, ChromeState.pick_project,
                "All projects", "Project"),
        _select(ChromeState.source, ChromeState.sources, ChromeState.pick_source,
                "All sources", "Source"),
        rx.el.div(
            *[
                rx.el.button(
                    RANGE_LABELS[key],
                    on_click=ChromeState.pick_range(key),  # type: ignore[operator]
                    aria_pressed=ChromeState.range == key,
                    type="button",
                )
                for key in RANGES
            ],
            role="group",
            aria_label="Time range",
        ),
        class_name="agentlens-chrome",
        on_mount=ChromeState.load_chrome,
    )  # fmt: skip
