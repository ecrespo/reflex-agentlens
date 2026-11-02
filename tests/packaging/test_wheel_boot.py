"""The released wheel boots with no JS runtime (plan task P7-01.2; REQ-200.b, REQ-210.b, DD-002,
DD-018).

Runs in the CI ``wheel`` job: ``AGENTLENS_WHEEL`` names the wheel ``uv build`` produced (Bun was
needed to build it). The test installs it into a fresh venv with ``uv``, starts ``serve`` with a
``PATH`` that holds only that venv (so no ``node`` or ``bun``), and requires the UI, with the CSP
of the shipped ``csp.json``, and an accepted OTLP span within 30 s of the process start.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.contract.ingest_rig import now_request
from tests.integration.live import free_port
from tests.unit.ingest import otlp

pytestmark = pytest.mark.packaging

BOOT_BUDGET_S = 30.0  # REQ-210.b


def _install(wheel: Path, venv: Path) -> Path:
    uv = shutil.which("uv")
    assert uv is not None, "uv is needed to create the venv"
    subprocess.run([uv, "venv", "-q", "--python", sys.executable, str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run([uv, "pip", "install", "-q", "--python", str(python), str(wheel)], check=True)
    return venv / "bin"


def _shipped_hashes(bindir: Path) -> list[str]:
    out = subprocess.run(
        [str(bindir / "python"), "-c",
         "import reflex_agentlens._webapp.build as b; print(b.frontend_dir())"],
        check=True, capture_output=True, text=True, env={"PATH": str(bindir)},
    ).stdout.strip()  # fmt: skip
    frontend = Path(out)
    assert (frontend / "index.html").is_file(), "the wheel carries no frontend"
    hashes = json.loads((frontend / "csp.json").read_text())["script_hashes"]
    assert hashes, "csp.json lists no inline-script hash"
    return list(hashes)


def _span_request() -> bytes:
    root = otlp.span(
        trace=0xB007, sid=1, name="invoke_agent boot", start_ago_s=2, end_ago_s=1,
        gen_ai__operation__name="invoke_agent", gen_ai__agent__name="boot",
    )  # fmt: skip
    return otlp.to_otlp_json(now_request(root))


def test_wheel_serves_ui_and_ingest_without_node_req200b_210b(tmp_path: Path) -> None:
    wheel = Path(os.environ["AGENTLENS_WHEEL"]).resolve()
    assert wheel.is_file(), wheel
    bindir = _install(wheel, tmp_path / "venv")
    hashes = _shipped_hashes(bindir)
    path = str(bindir)
    assert shutil.which("node", path=path) is None
    assert shutil.which("bun", path=path) is None

    port, data, log = free_port(), tmp_path / "data", tmp_path / "serve.log"
    env = {"PATH": path, "HOME": str(tmp_path), "AGENTLENS_SERVER_DATA_DIR": str(data)}
    base = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [str(bindir / "reflex-agentlens"), "serve", "--port", str(port), "--no-grpc"],
            env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=tmp_path,
        )  # fmt: skip
    try:
        page = ingest = None
        while time.monotonic() - started < BOOT_BUDGET_S + 30:  # report the real time if late
            if proc.poll() is not None:
                raise AssertionError(f"serve exited {proc.returncode}: {log.read_text()}")
            try:
                page = httpx.get(f"{base}/", timeout=2)
                key = (data / "bootstrap-api-key").read_text().strip()
                ingest = httpx.post(
                    f"{base}/v1/traces",
                    content=_span_request(),
                    headers={"content-type": "application/json",
                             "authorization": f"Bearer {key}"},
                    timeout=5,
                )  # fmt: skip
                if page.status_code == 200 and ingest.status_code == 200:
                    break
            except (httpx.TransportError, FileNotFoundError):
                pass
            time.sleep(0.25)
        elapsed = time.monotonic() - started
        assert page is not None
        assert ingest is not None, log.read_text()
        assert page.status_code == 200, page.text
        assert ingest.status_code == 200, ingest.text
        assert elapsed <= BOOT_BUDGET_S, f"UI + ingest after {elapsed:.1f}s"
        assert "<html" in page.text.lower()
        policy = page.headers["content-security-policy"]
        assert all(h in policy for h in hashes)
        assert "unsafe-eval" not in policy
        print(f"wheel boot: UI and ingest ready after {elapsed:.1f}s")  # noqa: T201 - CI timing
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0, log.read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
