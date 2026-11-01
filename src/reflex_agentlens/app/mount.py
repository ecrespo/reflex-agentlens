"""``mount()``: embed agentlens in a host Reflex app (plan task P6-03.2; API §7.1, REQ-190, 191).

Registers the pages under ``prefix``, the auth hook and a lifespan task that opens storage and
starts the services (``app.lifespan.runtime``). The optional OTLP/HTTP and health routes go in
front of Reflex's own backend through ``api_transformer``. Nothing here performs I/O: storage
opens when the host app starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Collection
from contextlib import asynccontextmanager
from typing import Any

import reflex as rx
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount
from starlette.types import ASGIApp

from reflex_agentlens.app.lifespan import runtime
from reflex_agentlens.app.routes import Health, health_routes
from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.ingest.engine import IngestEngine
from reflex_agentlens.ingest.http import otlp_routes
from reflex_agentlens.pages import ALL_PAGES, PAGES
from reflex_agentlens.views.registry import AuthHook, registry

__all__ = ["install", "mount", "page_route"]

_MOUNTED = "_agentlens_mounted"


def page_route(prefix: str, route: str) -> str:
    """``prefix`` + a relative page route (``/`` maps to the prefix itself)."""
    base = prefix.rstrip("/")
    if route == "/":
        return base or "/"
    return f"{base}{route}"


def _check_prefix(prefix: str, field: str) -> str:
    if not prefix.startswith("/") or "//" in prefix or any(c in prefix for c in "?#[] "):
        raise ValueError(f"{field} must be an absolute path such as /agentlens")
    return prefix.rstrip("/") or "/"


def _embedded_config(cfg: AgentlensConfig, *, ingest: bool, grpc: bool) -> AgentlensConfig:
    server = cfg.server.model_copy(update={"ingest_enabled": ingest, "grpc_enabled": grpc})
    return cfg.model_copy(update={"server": server})


class _RegistryEngine:
    """The OTLP routes' engine: the one the lifespan created (it does not exist yet)."""

    def __getattr__(self, name: str) -> Any:
        rt = registry().runtime
        if rt is None or rt.engine is None:
            raise AttributeError(name)
        return getattr(rt.engine, name)


def _current_health() -> Health | None:
    rt = registry().runtime
    return None if rt is None else rt.health


def mount(
    app: rx.App,
    *,
    auth: AuthHook,
    config: AgentlensConfig | None = None,
    prefix: str = "/agentlens",
    login_route: str | None = None,
    ingest: bool = False,
    ingest_prefix: str = "/agentlens/otlp",
    grpc: bool = False,
    health: bool = False,
    pages: Collection[str] = ALL_PAGES,
    demo: bool = False,
    nav: bool = True,
) -> None:
    """API §7.1. Calling it twice on one app raises ``RuntimeError``."""
    install(
        app, auth=auth, config=config, prefix=prefix, login_route=login_route, ingest=ingest,
        ingest_prefix=ingest_prefix, grpc=grpc, health=health, pages=pages, demo=demo, nav=nav,
    )  # fmt: skip


def install(
    app: rx.App,
    *,
    auth: AuthHook,
    config: AgentlensConfig | None = None,
    prefix: str = "/agentlens",
    login_route: str | None = None,
    ingest: bool = False,
    ingest_prefix: str = "/agentlens/otlp",
    grpc: bool = False,
    health: bool = False,
    pages: Collection[str] = ALL_PAGES,
    demo: bool = False,
    nav: bool = True,
    wrap: Callable[[Callable[[], rx.Component]], Callable[[], rx.Component]] | None = None,
    lifespan: bool = True,
) -> None:
    """:func:`mount` plus two knobs for the standalone app (P6-11): ``wrap`` puts a shell
    around every page, and ``lifespan=False`` leaves opening storage to the caller (``serve``
    runs the runtime itself and hands it to the registry)."""
    if getattr(app, _MOUNTED, False):
        raise RuntimeError("reflex_agentlens.mount() was already called on this app")
    if not callable(auth):
        raise TypeError("auth must be an async callable returning Viewer | None")
    prefix = _check_prefix(prefix, "prefix")
    unknown = set(pages) - ALL_PAGES
    if unknown:
        raise ValueError(f"unknown pages: {sorted(unknown)} (choose from {sorted(ALL_PAGES)})")
    if grpc and not ingest:
        raise ValueError("grpc=True needs ingest=True")

    reg = registry()
    reg.auth, reg.login_route, reg.prefix = auth, login_route, prefix
    reg.demo, reg.nav, reg.pages = demo, nav, frozenset(pages)
    if config is not None:
        reg.config = config

    for name in sorted(pages):
        page = PAGES[name]
        app.add_page(
            page.component if wrap is None else wrap(page.component),
            route=page_route(prefix, page.route),
            title=f"{page.title} · agentlens",
            on_load=page.on_load,
        )

    if lifespan:

        @asynccontextmanager
        async def agentlens_lifespan() -> AsyncIterator[None]:
            cfg = reg.config if reg.config is not None else load_config()
            cfg = _embedded_config(cfg, ingest=ingest, grpc=grpc)
            reg.config = cfg
            async with runtime(cfg, "demo" if demo else "serve") as rt:
                reg.runtime = rt
                try:
                    yield
                finally:
                    reg.runtime = None

        app.register_lifespan_task(agentlens_lifespan)

    routes: list[BaseRoute] = []
    if health:
        routes.extend(health_routes(_current_health))
    if ingest:
        engine: IngestEngine = _RegistryEngine()  # type: ignore[assignment]
        routes.extend(
            otlp_routes(
                engine, reg.config or load_config(), _check_prefix(ingest_prefix, "ingest_prefix")
            )
        )
    if routes:
        _add_transformer(app, routes)
    setattr(app, _MOUNTED, True)


def _add_transformer(app: rx.App, routes: list[BaseRoute]) -> None:
    def transform(inner: ASGIApp) -> ASGIApp:
        return Starlette(routes=[*routes, Mount("/", app=inner)])

    current: Any = app.api_transformer
    if current is None:
        app.api_transformer = transform
    elif isinstance(current, list | tuple):
        app.api_transformer = [*current, transform]
    else:
        app.api_transformer = [current, transform]
