"""Helpers of the Docker suite (plan tasks P7-02.2, P7-03.1; REQ-203, 204, 326, 364).

``AGENTLENS_IMAGE`` names the image under test (the CI ``docker`` job builds it from the
``Dockerfile``); every test is skipped without it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from tests.contract.ingest_rig import now_request
from tests.unit.ingest import otlp

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("probe_runs.py")


def image() -> str:
    return os.environ["AGENTLENS_IMAGE"]


def docker(
    *args: str, check: bool = True, stdin: str | None = None, env: dict[str, str] | None = None
) -> str:
    exe = shutil.which("docker")
    assert exe is not None, "the docker CLI is not on PATH"
    done = subprocess.run(  # fixed argv, no shell
        [exe, *args], check=False, capture_output=True, text=True, input=stdin,
        env=None if env is None else os.environ | env,
    )  # fmt: skip
    if check and done.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {done.stderr}")
    return done.stdout


def wait_healthy(container: str, timeout_s: float) -> float:
    """Seconds until Docker reports ``healthy``; fails on ``unhealthy``, exit or timeout."""
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        state = json.loads(docker("inspect", "-f", "{{json .State}}", container))
        health = (state.get("Health") or {}).get("Status")
        if health == "healthy":
            return time.monotonic() - started
        if health == "unhealthy" or not state["Running"]:
            raise AssertionError(f"{container}: {health}\n{docker('logs', container, check=False)}")
        time.sleep(1)
    raise AssertionError(f"{container} not healthy after {timeout_s}s")


def send_run(base: str, key: str, agent: str) -> None:
    root = otlp.span(
        trace=0xD0C, sid=1, name=f"invoke_agent {agent}", start_ago_s=3, end_ago_s=1,
        gen_ai__operation__name="invoke_agent", gen_ai__agent__name=agent,
    )  # fmt: skip
    response = httpx.post(
        f"{base}/v1/traces",
        content=otlp.to_otlp_json(now_request(root)),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        timeout=10,
    )
    assert response.status_code == 200, response.text


def run_agents(container: str, timeout_s: float = 20) -> list[str]:
    """Agent names in the run-list query, polled until one shows up (group commit)."""
    deadline = time.monotonic() + timeout_s
    while True:
        out = docker("exec", "-i", container, "python", "-", stdin=PROBE.read_text())
        agents = [run["agent_name"] for run in json.loads(out)]
        if agents or time.monotonic() > deadline:
            return agents
        time.sleep(0.5)
