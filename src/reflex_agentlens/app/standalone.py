"""``standalone_app()``: the exported pages behind the standalone shell and auth (plan task
P6-11.1; API §7.1, §7.3, REQ-192, REQ-224, DD-002).

The standalone app is :func:`app.mount.install` on a fresh ``rx.App``: the same pages a host
mounts, wrapped in :func:`components.shell.shell`, plus a login page. Authentication is the
built-in hook of API §7.3, which reads the browser's cookie and peer address from the Reflex
router and asks :func:`app.auth.standalone_viewer` (the code the login routes are tested with).

The page set is the same for every auth mode, because the frontend is compiled once at build
time (DD-002). The mode only decides, at runtime, whether ``/login`` is the redirect target;
in ``none`` and ``proxy`` mode the login page sends the browser to the dashboard.

Building the app performs no I/O: the configuration is read and the session secret resolved
the first time the hook runs.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import reflex as rx
from reflex.event import EventSpec

from reflex_agentlens.app.auth import standalone_viewer
from reflex_agentlens.app.bootstrap import session_secret
from reflex_agentlens.app.login import LOGIN_PAGE, LOGIN_PATH, safe_next
from reflex_agentlens.app.mount import install
from reflex_agentlens.app.policy import effective_auth_mode
from reflex_agentlens.components.shell import PostForm, shell
from reflex_agentlens.components.theme import root
from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.pages._base import AgentlensState
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.registry import registry

__all__ = [
    "LOGIN_TITLE",
    "LoginState",
    "StandaloneAuth",
    "build_standalone",
    "login_page",
    "login_route_for",
    "standalone_app",
]

LOGIN_TITLE = "Sign in · agentlens"
WRONG_CREDENTIALS = "Wrong username or password."


def _config() -> AgentlensConfig:
    reg = registry()
    if reg.config is None:
        reg.config = load_config()
    return reg.config


def login_route_for(cfg: AgentlensConfig) -> str | None:
    """REQ-191.b: unauthenticated visitors go to ``/login`` in ``password`` mode only."""
    return LOGIN_PAGE if effective_auth_mode(cfg) == "password" else None


class StandaloneAuth:
    """The built-in auth hook (API §7.3). The session key is resolved on first use."""

    def __init__(self) -> None:
        self._key: bytes | None = None

    def _session_key(self, cfg: AgentlensConfig) -> bytes:
        if self._key is None:
            self._key = session_secret(cfg)
        return self._key

    async def __call__(self, state: Any) -> Viewer | None:
        cfg = _config()
        headers = dict(state.router.headers.raw_headers)
        client_ip = str(state.router.session.client_ip or "") or None
        key = self._session_key(cfg) if effective_auth_mode(cfg) == "password" else b""
        return standalone_viewer(cfg, key, headers, client_ip)


# ── login page ───────────────────────────────────────────────────────────────────────────────


class LoginState(AgentlensState):
    next_path: str = "/"
    failed: bool = False

    @rx.event
    def load_login(self) -> EventSpec | None:
        params = parse_qs(urlsplit(str(self.router.url)).query)
        self.next_path = safe_next(_first(params.get("next")))
        self.failed = _first(params.get("error")) == "1"
        if registry().login_route is None:
            return rx.redirect(self.next_path)  # no password login in this mode
        return None


def _first(value: Any) -> str | None:
    if isinstance(value, list | tuple):
        return str(value[0]) if value else None
    return None if value is None else str(value)


def login_page() -> rx.Component:
    field = {"required": True, "auto_complete": "off"}
    return root(
        rx.el.main(
            rx.el.h1("Sign in to agentlens", class_name="agentlens-title"),
            rx.cond(
                LoginState.failed,
                rx.el.div(WRONG_CREDENTIALS, role="alert", class_name="agentlens-error"),
            ),
            PostForm.create(
                rx.el.label("Username", rx.el.input(name="username", type="text", **field)),
                rx.el.label("Password", rx.el.input(name="password", type="password", **field)),
                rx.el.input(type="hidden", name="next", value=LoginState.next_path),
                rx.el.button("Sign in", type="submit"),
                action=LOGIN_PATH,
                method="post",
                class_name="agentlens-login",
            ),
        )
    )


# ── the app ──────────────────────────────────────────────────────────────────────────────────


def build_standalone(
    config: AgentlensConfig | None = None, *, lifespan: bool = True, demo: bool = False
) -> rx.App:
    """The standalone app. ``serve`` passes ``lifespan=False`` because it opens storage itself
    and registers the runtime (P6-11.2); everything else matches :func:`standalone_app`."""
    app = rx.App()
    install(
        app,
        auth=StandaloneAuth(),
        config=config,
        prefix="/",
        login_route=None if config is None else login_route_for(config),
        demo=demo,
        wrap=shell,
        lifespan=lifespan,
    )
    app.add_page(login_page, route=LOGIN_PAGE, title=LOGIN_TITLE, on_load=LoginState.load_login)
    if config is None and lifespan:
        app.register_lifespan_task(_resolve_login_route)
    return app


def _resolve_login_route() -> None:
    """Without a config at build time, the login target follows the loaded one."""
    registry().login_route = login_route_for(_config())


def standalone_app(config: AgentlensConfig | None = None) -> rx.App:
    """API §7.1 / REQ-192.a: ``mount()`` on a fresh ``rx.App`` with the standalone shell and
    the standalone auth hooks (§7.3)."""
    return build_standalone(config)
