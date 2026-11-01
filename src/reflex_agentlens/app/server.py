"""The Starlette app of ``serve`` and ``collect`` (plan tasks P5-01.2, P5-03.4; DD-001).

:func:`build_app` wires the operational routes (``/healthz``, ``/readyz``) and, when this process
ingests, the OTLP/HTTP routes onto one Starlette app whose lifespan is :func:`app.lifespan.runtime`.
``serve`` gets its login routes in P5-04.2 and its Reflex mount in P6-11.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TextIO

from starlette.applications import Starlette
from starlette.routing import BaseRoute

from reflex_agentlens.app.backend import Mode
from reflex_agentlens.app.lifespan import Runtime, runtime
from reflex_agentlens.app.routes import Health, health_routes
from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.ingest.engine import IngestEngine
from reflex_agentlens.ingest.http import otlp_routes

__all__ = ["AppHandle", "build_app"]


class AppHandle:
    """The app plus the :class:`Runtime` once its lifespan entered (tests and the CLI read it)."""

    def __init__(self, app: Starlette) -> None:
        self.app = app
        self.runtime: Runtime | None = None


class _LateEngine:
    """Hands the OTLP routes the engine created by the lifespan (it does not exist yet)."""

    def __init__(self, handle: AppHandle) -> None:
        self._handle = handle

    def __getattr__(self, name: str) -> Any:
        rt = self._handle.runtime
        if rt is None or rt.engine is None:
            raise AttributeError(name)
        return getattr(rt.engine, name)


def build_app(
    cfg: AgentlensConfig,
    mode: Mode,
    *,
    banner: TextIO | None = None,
    runtime_options: dict[str, Any] | None = None,
    extra_routes: Callable[[AppHandle], list[BaseRoute]] | None = None,
) -> AppHandle:
    """One app per process; ``runtime_options`` go to :func:`runtime` (tests: backend options)."""
    options = runtime_options or {}

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with runtime(cfg, mode, banner=banner, **options) as rt:
            handle.runtime = rt
            try:
                yield
            finally:
                handle.runtime = None

    app = Starlette(lifespan=lifespan)
    handle = AppHandle(app)

    def health() -> Health | None:
        rt = handle.runtime
        return None if rt is None else rt.health

    routes: list[BaseRoute] = list(health_routes(health))
    if mode in ("serve", "collect") and cfg.server.ingest_enabled and not cfg.demo.enabled:
        engine: IngestEngine = _LateEngine(handle)  # type: ignore[assignment]
        routes.extend(otlp_routes(engine, cfg))
    if extra_routes is not None:
        routes.extend(extra_routes(handle))
    app.router.routes.extend(routes)
    return handle
