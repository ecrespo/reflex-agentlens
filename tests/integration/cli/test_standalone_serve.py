"""``serve`` with the built frontend, as a real process (plan task P6-11.2; REQ-192, 193.a,
REQ-200.b, DD-002).

Needs ``reflex_agentlens/_frontend`` (``uv run python -m reflex_agentlens._webapp.build``). The
test is skipped without it, except where ``AGENTLENS_REQUIRE_FRONTEND=1`` (the CI ``standalone``
job, which builds the frontend first) turns the skip into a failure.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from reflex_agentlens.app.webserve import bundle_dir
from tests.integration.live import free_port


def _require_bundle() -> None:
    if bundle_dir() is None:
        if os.environ.get("AGENTLENS_REQUIRE_FRONTEND") == "1":
            pytest.fail("the frontend is not built (AGENTLENS_REQUIRE_FRONTEND=1)")
        pytest.skip("frontend not built: uv run python -m reflex_agentlens._webapp.build")


def _wait(base: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"exited {proc.returncode}: {log.read_text()}")
        try:
            if httpx.get(f"{base}/readyz", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"not ready: {log.read_text()}")


def test_serve_answers_the_ui_with_csp_req_193a(tmp_path: Path) -> None:
    _require_bundle()
    port, log = free_port(), tmp_path / "serve.log"
    data = tmp_path / "data"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REFLEX_", "AGENTLENS_"))}
    env.update(HOME=str(tmp_path), AGENTLENS_SERVER_DATA_DIR=str(data))
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "reflex_agentlens.cli.main", "serve", "--port", str(port),
             "--no-grpc"],
            env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=tmp_path,
        )  # fmt: skip
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base, proc, log)
        page = httpx.get(f"{base}/", timeout=10)
        assert page.status_code == 200
        assert "<html" in page.text.lower()
        policy = page.headers["content-security-policy"]
        assert "'sha256-" in policy
        assert "unsafe-eval" not in policy
        assert f"connect-src 'self' ws://127.0.0.1:{port};" in policy
        assert httpx.get(f"{base}/healthz", timeout=5).json() == {"status": "ok"}
        assert httpx.get(f"{base}/ping", timeout=5).text == '"pong"'  # Reflex's backend
        # DD-002: the bundle was copied and pointed at this port; Reflex wrote nothing in cwd
        env_js = next((data / "web").glob("*/.web/build/client/assets/reflex-env-*.js"))
        assert f"ws://localhost:{port}/_event" in env_js.read_text()
        assert not (tmp_path / "assets").exists()
        assert not (tmp_path / ".states").exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0, log.read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
