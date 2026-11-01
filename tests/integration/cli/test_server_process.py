"""``serve`` and ``collect`` as real processes (plan task P5-03.4; REQ-200.a, 004.a/b, 201.a).

The process is ``python -m reflex_agentlens.cli.main``; the test waits for ``/readyz``, exports
one span with the key, sends SIGTERM and expects exit code 0.
"""

from __future__ import annotations

import asyncio
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from reflex_agentlens.app.bootstrap import KEY_FILE
from reflex_agentlens.cli.main import main
from tests.contract.ingest_rig import now_request
from tests.integration.app.conftest import Fresh
from tests.integration.cli.test_cli import project_with_runs
from tests.integration.live import free_port
from tests.unit.ingest import otlp

PB = "application/x-protobuf"


def output(log: Path) -> str:
    return log.read_text()


def spawn(fresh: Fresh, *argv: str, log: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, **fresh.env(AGENTLENS_LOGGING_LEVEL="INFO")}
    env = {k: v for k, v in env.items() if k != "AGENTLENS_TEST_PG_URL"}
    with log.open("wb") as fh:
        return subprocess.Popen(
            [sys.executable, "-m", "reflex_agentlens.cli.main", *argv],
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            cwd=fresh.tmp_path,
        )


async def wait_ready(base: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + 30
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"exited {proc.returncode}: {output(log)}")
            try:
                if (await client.get(f"{base}/readyz")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise AssertionError(f"not ready: {output(log)}")


async def export_and_stop(proc: subprocess.Popen[bytes], base: str, key: str, log: Path) -> None:
    await wait_ready(base, proc, log)
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{base}/healthz")).json() == {"status": "ok"}
        body = now_request(otlp.span(trace=7, sid=1)).SerializeToString()
        headers = {"content-type": PB, "authorization": f"Bearer {key}"}
        response = await client.post(f"{base}/v1/traces", content=body, headers=headers)
        assert response.status_code == 200, response.text
    proc.send_signal(signal.SIGTERM)
    assert await asyncio.to_thread(proc.wait, 30) == 0, output(log)


async def test_serve_on_sqlite_bootstraps_and_ingests_req_200a_201a(fresh: Fresh) -> None:
    if fresh.name != "sqlite":
        pytest.skip("serve on PostgreSQL is covered by the runtime tests")
    port, log = free_port(), fresh.tmp_path / "serve.log"
    proc = spawn(fresh, "serve", "--port", str(port), "--no-grpc", log=log)
    try:
        base = f"http://127.0.0.1:{port}"
        await wait_ready(base, proc, log)
        key = output(fresh.tmp_path / "data" / KEY_FILE).strip()
        await export_and_stop(proc, base, key, log)
    finally:
        proc.kill() if proc.poll() is None else None
    printed = output(log)
    assert f"UI http://127.0.0.1:{port}" in printed  # the banner
    assert key not in printed  # not a TTY: the key went to the 0600 file only


async def test_collect_on_postgres_ingests_req_004b(fresh: Fresh) -> None:
    if fresh.name != "pg":
        pytest.skip("collect refuses SQLite (see test_cli)")
    await project_with_runs(fresh, "collect-me")
    out = io.StringIO()
    argv = ["keys", "create", "--project", "collect-me"]
    assert await asyncio.to_thread(main, argv, env=fresh.env(), stdout=out) == 0
    key = next(ln for ln in out.getvalue().splitlines() if "(shown once)" in ln).rsplit(" ", 1)[1]
    port, log = free_port(), fresh.tmp_path / "collect.log"
    proc = spawn(fresh, "collect", "--port", str(port), "--no-grpc", log=log)
    try:
        await export_and_stop(proc, f"http://127.0.0.1:{port}", key, log)
    finally:
        proc.kill() if proc.poll() is None else None
    assert key not in output(log)
