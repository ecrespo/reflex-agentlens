"""Browser smoke suite fixtures (plan task P6-12.1; REQ-190.d, 193.a, 324, 327, 363).

Two real processes, started once per session:

- ``standalone``: ``reflex-agentlens serve`` on the exported frontend (``_webapp.build`` must
  have run), seeded over OTLP/HTTP with one agent Run whose LLM messages carry an XSS payload.
- ``host``: the minimal host app in ``tests/smoke/host`` (colliding state names and routes,
  agentlens mounted under ``/agentlens``), run with ``reflex run --env prod --single-port``, which
  compiles and builds it (Bun).

Every page is opened through :class:`Watch`, which records console errors, uncaught exceptions,
CSP violations, dialogs and any request that leaves the app's origin (REQ-324).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, Request, sync_playwright

from reflex_agentlens.app.webserve import bundle_dir
from tests.contract.ingest_rig import now_request
from tests.integration.live import free_port
from tests.unit.ingest import otlp

HOST_DIR = Path(__file__).parent / "host"
XSS_TRACE = 0xABC
XSS_SPAN = 2
EVIL_HOST = "evil.example"
XSS = (
    '<script>window.__xss=1</script><img src=x onerror="window.__xss=2"> '
    "[click](javascript:window.__xss=3) ![t](https://evil.example/p.png) "
    '<iframe src="https://evil.example/f"></iframe>'
)
CSP_PROBE = (
    "document.addEventListener('securitypolicyviolation', e =>"
    " console.error('CSP violation: ' + e.violatedDirective + ' ' + e.blockedURI))"
)


@dataclass
class Server:
    base: str
    proc: subprocess.Popen[bytes]
    log: Path
    data: Path
    key: str = ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _env(tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REFLEX_", "AGENTLENS_"))}
    env.update(HOME=str(tmp), AGENTLENS_SERVER_DATA_DIR=str(tmp / "data"))
    return env


def _wait(url: str, proc: subprocess.Popen[bytes], log: Path, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"exited {proc.returncode}: {log.read_text()[-4000:]}")
        try:
            if httpx.get(url, timeout=2, follow_redirects=True).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"not ready after {timeout_s}s: {log.read_text()[-4000:]}")


def _seed(server: Server) -> None:
    msgs = [{"role": "user", "parts": [{"type": "text", "content": XSS}]}]
    reply = [{"role": "assistant", "parts": [{"type": "text", "content": "**ok** " + XSS}]}]
    root = otlp.span(trace=XSS_TRACE, sid=1, name="invoke_agent shop", start_ago_s=10,
                     end_ago_s=1, gen_ai__operation__name="invoke_agent",
                     gen_ai__agent__name="shop")  # fmt: skip
    llm = otlp.span(trace=XSS_TRACE, sid=XSS_SPAN, parent=1, name="chat gpt-4o", start_ago_s=9,
                    end_ago_s=2, gen_ai__operation__name="chat",
                    gen_ai__request__model="gpt-4o", gen_ai__system="openai",
                    gen_ai__usage__input_tokens=10, gen_ai__usage__output_tokens=5,
                    gen_ai__input__messages=json.dumps(msgs),
                    gen_ai__output__messages=json.dumps(reply))  # fmt: skip
    response = httpx.post(
        f"{server.base}/v1/traces",
        content=otlp.to_otlp_json(now_request(root, llm)),
        headers={"content-type": "application/json", "authorization": f"Bearer {server.key}"},
        timeout=10,
    )
    assert response.status_code == 200, response.text


@pytest.fixture(scope="session")
def standalone(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    if bundle_dir() is None:
        pytest.fail("the frontend is not built: uv run python -m reflex_agentlens._webapp.build")
    tmp = tmp_path_factory.mktemp("standalone")
    port, log = free_port(), tmp / "serve.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "reflex_agentlens.cli.main", "serve", "--port", str(port),
             "--no-grpc"],
            env=_env(tmp), stdout=fh, stderr=subprocess.STDOUT, cwd=tmp,
        )  # fmt: skip
    server = Server(f"http://127.0.0.1:{port}", proc, log, tmp / "data")
    try:
        _wait(f"{server.base}/readyz", proc, log, 60)
        server.key = (server.data / "bootstrap-api-key").read_text().strip()
        _seed(server)
        time.sleep(1.0)  # the writer's group commit
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def host(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    tmp = tmp_path_factory.mktemp("host")
    app_dir = tmp / "host"
    shutil.copytree(HOST_DIR, app_dir, ignore=shutil.ignore_patterns("__pycache__"))
    port, log = free_port(), tmp / "host.log"
    env = _env(tmp) | {"AGENTLENS_AUTH_MODE": "none"}
    reflex = Path(sys.executable).with_name("reflex")
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [str(reflex), "run", "--env", "prod", "--single-port", "--backend-port", str(port),
             "--backend-host", "127.0.0.1"],
            env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=app_dir,
        )  # fmt: skip
    server = Server(f"http://127.0.0.1:{port}", proc, log, tmp / "data")
    try:
        _wait(f"{server.base}/runs", proc, log, 400)
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            yield b
        finally:
            b.close()


@dataclass
class Watch:
    """What a page did that it must not: errors, CSP violations, dialogs, outbound requests."""

    origin: str
    errors: list[str] = field(default_factory=list)
    outbound: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)

    def request(self, request: Request) -> None:
        url = request.url
        if url.startswith(("data:", "blob:")):
            return
        if not url.startswith((self.origin, self.origin.replace("127.0.0.1", "localhost"))):
            self.outbound.append(url)

    def assert_clean(self) -> None:
        assert self.errors == [], self.errors
        assert self.outbound == [], self.outbound
        assert self.dialogs == [], self.dialogs


def watched(browser: Browser, origin: str) -> tuple[Page, Watch]:
    page = browser.new_page()
    watch = Watch(origin)
    page.add_init_script(CSP_PROBE)
    page.on("console", lambda m: watch.errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: watch.errors.append(f"pageerror: {e}"))
    page.on("request", watch.request)

    def on_dialog(dialog: object) -> None:
        watch.dialogs.append(getattr(dialog, "message", ""))
        dialog.dismiss()  # type: ignore[attr-defined]

    page.on("dialog", on_dialog)
    return page, watch
