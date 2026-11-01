"""The Starlette app of ``serve`` and ``collect`` (plan tasks P5-01.2, P5-03.4; DD-001).

:func:`build_app` wires the operational routes (``/healthz``, ``/readyz``) and, when this process
ingests, the OTLP/HTTP routes onto one Starlette app whose lifespan is :func:`app.lifespan.runtime`.
``serve`` and ``demo`` add the login routes (``password`` mode) and CORS pinned to one origin,
and, from the CLI, the standalone Reflex app behind the Content-Security-Policy (P6-11.2,
:mod:`app.webserve`): it is mounted last, so the operational, OTLP and login routes win.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import signal
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO

from granian.constants import Interfaces
from granian.server.embed import Server as GranianServer
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from reflex_agentlens.app.backend import Mode
from reflex_agentlens.app.bootstrap import session_secret
from reflex_agentlens.app.lifespan import Runtime, ingests, runtime
from reflex_agentlens.app.login import cors_origin, login_routes
from reflex_agentlens.app.routes import Health, health_routes
from reflex_agentlens.app.webserve import NOT_BUILT, CspMiddleware, Ui, asgi_lifespan, load_ui
from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import AgentlensError
from reflex_agentlens.ingest.engine import IngestEngine
from reflex_agentlens.ingest.http import otlp_routes
from reflex_agentlens.views.registry import registry

__all__ = ["AppHandle", "PortInUse", "build_app", "probe_ports", "run_server"]

log = logging.getLogger("agentlens.app")


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
    ui: Ui | None = None,
) -> AppHandle:
    """One app per process; ``runtime_options`` go to :func:`runtime` (tests: backend options).
    ``ui`` (``serve``/``demo`` only) is mounted at ``/``; ``Ui(None)`` answers ``/`` with how to
    build the frontend."""
    options = runtime_options or {}
    ui_app = ui.app if ui is not None and mode in ("serve", "demo") else None

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with runtime(cfg, mode, banner=banner, **options) as rt:
            handle.runtime = rt
            if ui_app is not None:
                registry().runtime = rt  # the pages reach the services through the registry
            try:
                if ui_app is None:
                    yield
                else:
                    async with asgi_lifespan(ui_app):
                        yield
            finally:
                handle.runtime = None
                if ui_app is not None:
                    registry().runtime = None

    app = Starlette(lifespan=lifespan)
    handle = AppHandle(app)

    def health() -> Health | None:
        rt = handle.runtime
        return None if rt is None else rt.health

    routes: list[BaseRoute] = list(health_routes(health))
    if ingests(cfg, mode):
        engine: IngestEngine = _LateEngine(handle)  # type: ignore[assignment]
        routes.extend(otlp_routes(engine, cfg))
    if mode in ("serve", "demo"):
        routes.extend(login_routes(cfg, session_secret(cfg)))  # password mode only
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[cors_origin(cfg)],  # never "*" (Tech Design §6.1, A4)
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["authorization", "content-type", "content-encoding", "x-agentlens-key"],
        )
    if extra_routes is not None:
        routes.extend(extra_routes(handle))
    if ui is not None and mode in ("serve", "demo"):
        if ui_app is None:
            routes.append(Route("/", lambda _: PlainTextResponse(NOT_BUILT, 503)))
        else:
            routes.append(Mount("/", app=ui_app))
            app.add_middleware(CspMiddleware, cfg=cfg, script_hashes=ui.script_hashes)
    app.router.routes.extend(routes)
    return handle


# ── runner (P5-03.4, P5-05.1) ────────────────────────────────────────────────────────────────


class PortInUse(AgentlensError):
    """A configured port is taken (exit 3, REQ-200.c); names the port and the setting."""

    def __init__(self, host: str, port: int, setting: str, flag: str) -> None:
        self.port = port
        super().__init__(
            f"port {port} on {host} is already in use; change {setting} (or pass {flag})"
        )


def _probe(host: str, port: int, setting: str, flag: str) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ignore TIME_WAIT leftovers
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                raise PortInUse(host, port, setting, flag) from None
            raise


def probe_ports(cfg: AgentlensConfig, mode: Mode) -> None:
    """REQ-200.c: fail before anything starts when the HTTP or gRPC port is taken."""
    server = cfg.server
    _probe(server.http_host, server.http_port, "server.http_port", "--port")
    if ingests(cfg, mode) and server.grpc_enabled:
        host = server.grpc_host or server.http_host
        _probe(host, server.grpc_port, "server.grpc_port", "--grpc-port")


def _without_lifespan(app: Starlette) -> ASGIApp:
    """Granian drives the ASGI lifespan, but the runner already entered it: acknowledge only."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await app(scope, receive, send)

    return wrapped


async def run_server(
    cfg: AgentlensConfig,
    mode: Mode,
    *,
    banner: TextIO | None = None,
    runtime_options: dict[str, Any] | None = None,
    stop: asyncio.Event | None = None,
    ui: bool = False,
    frontend: Path | None = None,
) -> None:
    """Serve until SIGTERM/SIGINT (or ``stop``). Shutdown order (REQ-331): readiness and
    ``/healthz`` turn 503, ingest admission closes and the writer drains within
    ``shutdown_grace``, gRPC stops, then the HTTP server stops and the backend closes.
    ``ui=True`` (the CLI) serves the standalone frontend from ``frontend`` or the package."""
    probe_ports(cfg, mode)
    mounted = (
        load_ui(cfg, mode, bundle=frontend, banner=banner)
        if ui and mode in ("serve", "demo")
        else None
    )
    handle = build_app(cfg, mode, banner=banner, runtime_options=runtime_options, ui=mounted)
    server = GranianServer(
        _without_lifespan(handle.app),
        address=cfg.server.http_host,
        port=cfg.server.http_port,
        interface=Interfaces.ASGI,
        log_enabled=False,
    )
    loop = asyncio.get_running_loop()
    requested = stop or asyncio.Event()

    async def shutdown() -> None:
        await requested.wait()
        rt = handle.runtime
        if rt is not None:
            rt.health.shutting_down = True
            if rt.engine is not None:
                drained = await rt.engine.stop()
                if not drained:
                    log.warning("shutdown grace ran out before every accepted batch committed")
        server.stop()

    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        loop.add_signal_handler(sig, requested.set)
    try:
        async with handle.app.router.lifespan_context(handle.app):
            watcher = asyncio.create_task(shutdown())
            try:
                await server.serve()
            finally:
                requested.set()
                await watcher
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
