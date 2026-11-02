"""A minimal host app for the smoke suite (plan task P6-12.1; REQ-190.b/d).

It defines its own states with the same class names agentlens uses (``RunsState``,
``SettingsState``, ``AgentlensState``) and its own ``/runs`` and ``/settings`` routes, then mounts
agentlens under ``/agentlens``. Both must keep working side by side.
"""

from __future__ import annotations

from typing import Any

import reflex as rx

import reflex_agentlens as agentlens


class AgentlensState(rx.State):
    clicks: int = 0

    @rx.event
    def click(self) -> None:
        self.clicks += 1


class RunsState(rx.State):
    label: str = "host runs"


class SettingsState(rx.State):
    label: str = "host settings"


def runs() -> rx.Component:
    return rx.el.main(
        rx.el.h1(RunsState.label, id="host-title"),
        rx.el.button("click", id="host-click", on_click=AgentlensState.click),
        rx.el.span(AgentlensState.clicks, id="host-clicks"),
    )


def settings() -> rx.Component:
    return rx.el.main(rx.el.h1(SettingsState.label, id="host-title"))


async def allow(state: Any) -> agentlens.Viewer | None:
    return agentlens.Viewer("host-user", "Host user", is_admin=True)


app = rx.App()
app.add_page(runs, route="/runs")
app.add_page(settings, route="/settings")
agentlens.mount(app, auth=allow, prefix="/agentlens")
