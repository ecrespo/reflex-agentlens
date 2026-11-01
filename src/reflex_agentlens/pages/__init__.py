"""Reflex pages (API §7.2): ``dashboard``, ``runs``, ``trace``, ``sessions``, ``session``,
``settings``, and :data:`ROUTES`, the relative route of each.

Importing this package imports Reflex (it is reached through ``reflex_agentlens.pages`` only
when a host mounts pages); ``import reflex_agentlens`` alone does not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import reflex as rx

from reflex_agentlens.pages import (
    dashboard_page,
    runs_page,
    session_page,
    sessions_page,
    settings_page,
    trace_page,
)

__all__ = [
    "ALL_PAGES",
    "PAGES",
    "ROUTES",
    "Page",
    "PageName",
    "dashboard",
    "runs",
    "session",
    "sessions",
    "settings",
    "trace",
]

PageName = Literal["dashboard", "runs", "trace", "sessions", "session", "settings"]

ROUTES: dict[str, str] = {
    "dashboard": "/",
    "runs": "/runs",
    "trace": "/trace/[trace_id]",
    "sessions": "/sessions",
    "session": "/sessions/[session_id]",
    "settings": "/settings",
}
ALL_PAGES: frozenset[str] = frozenset(ROUTES)


@dataclass(frozen=True, slots=True)
class Page:
    name: str
    route: str
    title: str
    component: Callable[[], rx.Component]
    on_load: list[Any]


_MODULES = {
    "dashboard": dashboard_page,
    "runs": runs_page,
    "trace": trace_page,
    "sessions": sessions_page,
    "session": session_page,
    "settings": settings_page,
}
PAGES: dict[str, Page] = {
    name: Page(name, ROUTES[name], mod.TITLE, mod.page, list(mod.ON_LOAD))
    for name, mod in _MODULES.items()
}

dashboard = dashboard_page.page
runs = runs_page.page
trace = trace_page.page
sessions = sessions_page.page
session = session_page.page
settings = settings_page.page
