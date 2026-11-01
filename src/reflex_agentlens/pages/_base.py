"""Base state, event wrapper and page frame (plan tasks P6-01.2, P6-03.2; Tech Design §5.5).

Every agentlens state subclasses :class:`AgentlensState`; Reflex names states after their
module, so ours never collide with a host's (REQ-190.d). Page loads call :meth:`check_auth`
(the hook is never cached on a page load); data events are written with :func:`ui_event`, which
resolves the viewer under the A-12 rule, runs the body through ``_guard.guarded`` and turns
failures into the banner, "not authorized" or error states.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit

import reflex as rx
from reflex.event import EventSpec

from reflex_agentlens.components.theme import root
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.guard import UiError, guarded, resolve_viewer
from reflex_agentlens.views.registry import registry
from reflex_agentlens.views.snippets import quickstart_snippet

__all__ = ["AgentlensState", "empty_state", "page_frame", "ui_event"]

S = TypeVar("S", bound="AgentlensState")


class AgentlensState(rx.State):
    """Viewer and page status shared by every agentlens page."""

    authorized: bool = False
    checked: bool = False
    viewer_name: str = ""
    is_admin: bool = False
    error_kind: str = ""
    error_message: str = ""

    def _token(self) -> str:
        return str(self.router.session.client_token or "")

    async def _viewer(self, *, page_load: bool) -> Viewer | None:
        reg = registry()
        if reg.auth is None:
            return None  # mount() always installs a hook; without one nothing is shown
        return await resolve_viewer(
            reg.auth, self, self._token(), page_load=page_load, cache=reg.viewers
        )

    def _set_viewer(self, viewer: Viewer | None) -> None:
        self.checked = True
        self.authorized = viewer is not None
        self.viewer_name = viewer.display_name if viewer else ""
        self.is_admin = bool(viewer and viewer.is_admin)

    def _fail(self, error: UiError) -> None:
        self.error_kind, self.error_message = error.kind, error.message
        if error.kind == "forbidden":
            self.authorized = False

    def _deny(self) -> EventSpec | None:
        """REQ-191.b: no data; redirect to the login route, or the "not authorized" state."""
        self._set_viewer(None)
        login = registry().login_route
        if login is None:
            return None
        parts = urlsplit(str(self.router.url))
        here = parts.path + (f"?{parts.query}" if parts.query else "")
        return rx.redirect(f"{login}?next={quote(here, safe='/')}")

    @rx.event
    async def check_auth(self) -> EventSpec | None:
        """Page ``on_load``: always asks the hook (PRD v1.2 REQ-191.a)."""
        self.error_kind = self.error_message = ""
        viewer, error = await guarded(lambda: self._viewer(page_load=True))
        if error is not None:
            self._set_viewer(None)
            self._fail(error)
            return None
        if viewer is None:
            return self._deny()
        self._set_viewer(viewer)
        return None


Body = Callable[..., Awaitable[Any]]


def ui_event(fn: Body) -> Callable[..., Awaitable[Any]]:
    """Wrap a data event ``async def handler(self, viewer, *args)`` (Tech Design §5.5).

    The wrapper resolves the viewer (an event may reuse it ≤ 30 s, A-12), denies when there is
    none, clears the previous error, and maps every failure through ``_guard.classify``. Reflex
    sees the handler's own arguments without ``viewer``.
    """

    @functools.wraps(fn)
    async def handler(self: AgentlensState, *args: Any) -> Any:
        viewer, error = await guarded(lambda: self._viewer(page_load=False))
        if error is not None:
            self._fail(error)
            return None
        if viewer is None:
            return self._deny()
        self.error_kind = self.error_message = ""
        result, error = await guarded(lambda: fn(self, viewer, *args))
        if error is not None:
            self._fail(error)
            return None
        return result

    params = list(inspect.signature(fn).parameters.values())
    del params[1]  # ``viewer`` is supplied by the wrapper
    handler.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined,unused-ignore]
    del handler.__wrapped__
    return handler


# ── frame and inline states ──────────────────────────────────────────────────────────────────


def _banner() -> rx.Component:
    return rx.cond(
        AgentlensState.error_kind == "storage",
        rx.el.div(AgentlensState.error_message, role="status", class_name="agentlens-banner"),
    )


def _error() -> rx.Component:
    return rx.cond(
        (AgentlensState.error_kind != "") & (AgentlensState.error_kind != "storage"),
        rx.el.div(AgentlensState.error_message, role="alert", class_name="agentlens-error"),
    )


def not_authorized() -> rx.Component:
    return rx.el.div(
        rx.el.h2("Not authorized"),
        rx.el.p("You do not have access to agentlens with this account."),
        class_name="agentlens-denied",
    )


def empty_state() -> rx.Component:
    """REQ-130.b: no data yet, with the quickstart snippet (placeholder key)."""
    return rx.el.div(
        rx.el.h2("No traces yet"),
        rx.el.p("Point an OpenTelemetry exporter at this instance with a Project API key:"),
        rx.el.pre(rx.el.code(quickstart_snippet(registry().config))),
        class_name="agentlens-empty",
    )


def page_frame(title: str, body: rx.Component) -> rx.Component:
    """A page: theme root, banner, error state, and the body only once authorized (nothing is
    rendered from data before the auth hook has answered, REQ-191.b)."""
    return root(
        _banner(),
        rx.cond(
            AgentlensState.authorized,
            rx.fragment(rx.el.h1(title, class_name="agentlens-title"), _error(), body),
            rx.cond(AgentlensState.checked, not_authorized(), rx.fragment()),
        ),
    )
