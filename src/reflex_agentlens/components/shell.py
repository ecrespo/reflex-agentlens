"""Navigation shell of the standalone app (plan task P6-11.1; REQ-192.a).

The shell is a header around each exported page: the product name, one link per mounted page
(under the registry prefix) and, in ``password`` mode, a sign-out button. Sign-out is a plain
HTML form posting to the server route of P5-04.2, so the session cookie stays ``HttpOnly`` and
no script is involved. Hosts that embed the pages never see this shell (REQ-192.a: no page
exists only in standalone mode).
"""

from __future__ import annotations

from collections.abc import Callable

import reflex as rx
from reflex_components_core.el.elements.base import BaseHTML  # type: ignore[import-untyped]

from reflex_agentlens.app.login import LOGOUT_PATH
from reflex_agentlens.app.mount import page_route
from reflex_agentlens.pages import ROUTES
from reflex_agentlens.pages._base import AgentlensState
from reflex_agentlens.views.registry import registry

__all__ = ["NAV", "PostForm", "ShellState", "nav_links", "shell"]

NAV: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard"),
    ("runs", "Runs"),
    ("sessions", "Sessions"),
    ("settings", "Settings"),
)


class PostForm(BaseHTML):
    """A native ``<form method="post">``: ``rx.el.form`` always prevents the browser submit,
    and the login/logout routes need a real navigation to set or clear the cookie."""

    tag = "form"

    action: rx.Var[str]
    method: rx.Var[str]


class ShellState(AgentlensState):
    sign_out: bool = False

    @rx.event
    def load_shell(self) -> None:
        self.sign_out = registry().login_route is not None


def nav_links(prefix: str, pages: frozenset[str]) -> list[tuple[str, str]]:
    """``(label, href)`` of every mounted page that has a menu entry, in menu order."""
    return [(label, page_route(prefix, ROUTES[name])) for name, label in NAV if name in pages]


def _header() -> rx.Component:
    reg = registry()
    return rx.el.header(
        rx.el.a("agentlens", href=page_route(reg.prefix, "/"), class_name="agentlens-brand"),
        rx.el.nav(
            *[rx.link(label, href=href) for label, href in nav_links(reg.prefix, reg.pages)],
            aria_label="agentlens",
        ),
        rx.cond(
            ShellState.sign_out,
            PostForm.create(
                rx.el.button("Sign out", type="submit"), action=LOGOUT_PATH, method="post"
            ),
        ),
        class_name="agentlens-shell",
        on_mount=ShellState.load_shell,
    )


def shell(page: Callable[[], rx.Component]) -> Callable[[], rx.Component]:
    """``page`` with the standalone header above it."""

    def wrapped() -> rx.Component:
        return rx.fragment(_header(), page())

    wrapped.__name__ = getattr(page, "__name__", "page")
    wrapped.__qualname__ = getattr(page, "__qualname__", wrapped.__name__)
    return wrapped
