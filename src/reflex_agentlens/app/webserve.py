"""Serving the pre-built frontend from ``serve`` (plan task P6-11.2; DD-002, DD-018, REQ-193,
REQ-200.b).

The recipe of spike S1 (Tech Design §11.1), in the order ``serve`` runs it:

1. :func:`prepare_workdir` copies the exported bundle to ``{data_dir}/web/<version>/.web/build/
   client``, writes the runtime ``rxconfig.py`` (telemetry off, CORS pinned), rewrites the backend
   origin compiled into ``assets/reflex-env-*.js`` (``localhost:8000``) and regenerates its
   precompressed sibling, and hashes every inline ``<script>`` of the shipped HTML (DD-018).
2. :func:`enter_workdir` sets the Reflex environment and ``chdir``s there **before** Reflex is
   imported, so its import-time writes (``assets/external``, ``.states``) land in the data
   directory (DD-002 consequence 4).
3. :func:`load_ui` builds the standalone app (``lifespan=False``: ``serve`` opens storage itself)
   and returns its ASGI app, which ``app.server`` mounts after the operational routes, wrapped in
   :class:`CspMiddleware`.

Nothing here needs Node or Bun. Without a built frontend, :func:`load_ui` says how to build one.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reflex_agentlens import __version__
from reflex_agentlens._webapp.build import frontend_dir, rxconfig_source
from reflex_agentlens.app.backend import Mode
from reflex_agentlens.app.login import cors_origin
from reflex_agentlens.core.config import AgentlensConfig

__all__ = [
    "BAKED_ORIGIN",
    "NOT_BUILT",
    "CspMiddleware",
    "Ui",
    "Workdir",
    "asgi_lifespan",
    "bundle_dir",
    "csp_policy",
    "enter_workdir",
    "inline_script_hashes",
    "load_ui",
    "prepare_workdir",
    "reflex_env",
    "rewrite_backend_origin",
]

BAKED_ORIGIN = "localhost:8000"
NOT_BUILT = (
    "The agentlens UI frontend is not built, so only the API and OTLP routes are served. "
    "In a source checkout, build it with `uv run python -m reflex_agentlens._webapp.build` "
    "(needs Bun or Node), then restart `serve`. Released wheels and images include it."
)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_HOST = re.compile(r"^(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")
_PRECOMPRESSED = (".gz", ".br", ".zst")


@dataclass(frozen=True, slots=True)
class Workdir:
    root: Path  # {data_dir}/web/<version>
    client: Path  # root/.web/build/client
    script_hashes: tuple[str, ...]
    rewritten: int  # backend URLs pointed at the runtime origin


@dataclass(frozen=True, slots=True)
class Ui:
    """What ``serve`` mounts: the Reflex app, or ``None`` when the frontend is not built."""

    app: ASGIApp | None
    script_hashes: tuple[str, ...] = ()


def bundle_dir(path: Path | None = None) -> Path | None:
    """The exported frontend (``reflex_agentlens/_frontend`` by default), if it is built."""
    candidate = path if path is not None else frontend_dir()
    return candidate if (candidate / "index.html").is_file() else None


def inline_script_hashes(html: str) -> list[str]:
    """CSP source expressions (``'sha256-…'``) for every inline ``<script>`` of ``html``."""
    out = []
    for body in _INLINE_SCRIPT.findall(html):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        out.append(f"'sha256-{digest}'")
    return out


def _backend_origins(cfg: AgentlensConfig) -> tuple[str, str, str]:
    """``(netloc, http scheme, ws scheme)`` the browser should reach the backend on."""
    public = cfg.server.public_url
    if public is not None:
        parts = urlsplit(str(public))
        secure = parts.scheme == "https"
        return parts.netloc, "https" if secure else "http", "wss" if secure else "ws"
    return f"localhost:{cfg.server.http_port}", "http", "ws"


def rewrite_backend_origin(client: Path, cfg: AgentlensConfig) -> int:
    """DD-002 consequence 1: point the compiled backend URLs at the runtime origin. On plain
    HTTP the browser keeps the compiled port, so it must be the real one. Returns the number of
    URLs rewritten."""
    netloc, http, ws = _backend_origins(cfg)
    count = 0
    for module in sorted((client / "assets").glob("reflex-env-*.js")):
        text = module.read_text(encoding="utf-8")
        count += text.count(f"://{BAKED_ORIGIN}")
        new = text.replace(f"http://{BAKED_ORIGIN}", f"{http}://{netloc}").replace(
            f"ws://{BAKED_ORIGIN}", f"{ws}://{netloc}"
        )
        module.write_text(new, encoding="utf-8")
        # The static handler serves a precompressed sibling in preference to the file.
        for suffix in _PRECOMPRESSED:
            module.with_name(module.name + suffix).unlink(missing_ok=True)
        module.with_name(module.name + ".gz").write_bytes(gzip.compress(new.encode("utf-8")))
    return count


def _hashes(client: Path) -> tuple[str, ...]:
    found: set[str] = set()
    for page in client.rglob("*.html"):
        found.update(inline_script_hashes(page.read_text(encoding="utf-8")))
    return tuple(sorted(found))


def prepare_workdir(cfg: AgentlensConfig, bundle: Path, *, version: str = __version__) -> Workdir:
    """Copy ``bundle`` into the data directory and adapt it to this configuration."""
    data_dir = cfg.server.data_dir
    if data_dir is None:  # load_config always resolves it; a hand-built config may not
        raise ValueError("server.data_dir is not resolved")
    root = data_dir / "web" / version
    client = root / ".web" / "build" / "client"
    if client.exists():
        shutil.rmtree(client)
    client.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, client, ignore=shutil.ignore_patterns(".gitignore"))
    (root / "rxconfig.py").write_text(rxconfig_source(cors_origin=cors_origin(cfg)))
    rewritten = rewrite_backend_origin(client, cfg)
    return Workdir(root, client, _hashes(client), rewritten)


def reflex_env(workdir: Workdir) -> dict[str, str]:
    """DD-002 step 3: the environment that makes Reflex serve the pre-built bundle."""
    return {
        "REFLEX_WEB_WORKDIR": str(workdir.root / ".web"),
        "REFLEX_STATES_WORKDIR": str(workdir.root / ".states"),
        "REFLEX_ENV_MODE": "prod",
        "REFLEX_TELEMETRY_ENABLED": "false",
        "__REFLEX_MOUNT_FRONTEND_COMPILED_APP": "true",
        "__REFLEX_SKIP_COMPILE": "true",
    }


def enter_workdir(workdir: Workdir) -> None:
    """Set the environment, ``chdir`` and put the rxconfig on ``sys.path`` (before Reflex is
    imported; DD-002 consequence 4)."""
    os.environ.update(reflex_env(workdir))
    os.chdir(workdir.root)
    if str(workdir.root) not in sys.path:
        sys.path.insert(0, str(workdir.root))


def load_ui(
    cfg: AgentlensConfig, mode: Mode, *, bundle: Path | None = None, banner: TextIO | None = None
) -> Ui:
    """The standalone UI for ``serve``/``demo``; ``Ui(None)`` (and a hint) when not built."""
    source = bundle_dir(bundle)
    if source is None:
        if banner is not None:
            print(NOT_BUILT, file=banner, flush=True)
        return Ui(None)
    workdir = prepare_workdir(cfg, source)
    enter_workdir(workdir)
    from reflex_agentlens.app.standalone import build_standalone  # imports Reflex: last

    app = build_standalone(cfg, lifespan=False, demo=mode == "demo")
    return Ui(app(), workdir.script_hashes)


# ── Content-Security-Policy (REQ-193, API §7.4, DD-018) ──────────────────────────────────────


def csp_policy(script_hashes: Iterable[str], ws_origin: str | None, frame_ancestors: str) -> str:
    """The API §7.4 baseline. ``ws_origin`` is the websocket origin of this request, if known."""
    scripts = " ".join(["'self'", *sorted(set(script_hashes))])
    connect = "'self'" if ws_origin is None else f"'self' {ws_origin}"
    return (
        f"default-src 'self'; script-src {scripts}; connect-src {connect}; "
        "img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        f"frame-ancestors {frame_ancestors}"
    )


class CspMiddleware:
    """Adds the policy to every HTTP response. ``connect-src`` names the websocket origin of
    ``server.public_url`` or, without one, of the request's own ``Host`` (validated)."""

    def __init__(self, app: ASGIApp, cfg: AgentlensConfig, script_hashes: Iterable[str]) -> None:
        self.app = app
        self.hashes = tuple(script_hashes)
        self.frame_ancestors = cfg.auth.frame_ancestors
        public = cfg.server.public_url
        self.fixed: str | None = None
        if public is not None:
            parts = urlsplit(str(public))
            self.fixed = f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}"

    def _ws_origin(self, scope: Scope) -> str | None:
        if self.fixed is not None:
            return self.fixed
        host = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"host"), "")
        if not _HOST.match(host):
            return None
        return f"{'wss' if scope.get('scheme') == 'https' else 'ws'}://{host}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = (
            b"content-security-policy",
            csp_policy(self.hashes, self._ws_origin(scope), self.frame_ancestors).encode(),
        )

        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), header]}
            await send(message)

        await self.app(scope, receive, send_with_csp)


# ── lifespan of a mounted ASGI app ───────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def asgi_lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Run ``app``'s ASGI lifespan (Starlette does not forward it to mounted apps). Reflex
    starts its event processor there."""
    inbox: asyncio.Queue[Message] = asyncio.Queue()
    outbox: asyncio.Queue[Message] = asyncio.Queue()
    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}

    async def receive() -> Message:
        return await inbox.get()

    async def send(message: Message) -> None:
        await outbox.put(message)

    async def run() -> None:
        await app(scope, receive, send)

    task: asyncio.Task[None] = asyncio.create_task(run())

    async def reply(expected: str) -> None:
        getter = asyncio.create_task(outbox.get())
        done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
        if getter not in done:
            getter.cancel()
            task.result()  # raises the app's error
            raise RuntimeError(f"the UI app ended its lifespan before {expected}")
        message = getter.result()
        if message["type"] != f"{expected}.complete":
            raise RuntimeError(f"UI app {expected} failed: {message.get('message', '')}")

    await inbox.put({"type": "lifespan.startup"})
    try:
        await reply("lifespan.startup")
    except BaseException:
        task.cancel()
        raise
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        try:
            await reply("lifespan.shutdown")
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
