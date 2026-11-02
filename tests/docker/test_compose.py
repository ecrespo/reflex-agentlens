"""``compose.yaml``, the try-it topology (plan task P7-03.1; REQ-204, REQ-364, DD-021 T2)."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest

from tests.docker.conftest import ROOT, docker, image, run_agents, send_run
from tests.integration.live import free_port

pytestmark = pytest.mark.docker

COMPOSE = ROOT / "compose.yaml"


@dataclass
class Stack:
    project: str
    port: int
    env: dict[str, str] = field(default_factory=dict)

    def compose(self, *args: str) -> str:
        return docker("compose", "--project-name", self.project, "-f", str(COMPOSE), *args,
                      env=self.env)  # fmt: skip


@pytest.fixture
def stack() -> Iterator[Stack]:
    port = free_port()
    env = {
        "AGENTLENS_IMAGE": image(),
        "AGENTLENS_HTTP_PORT": str(port),
        "AGENTLENS_GRPC_PORT": str(free_port()),
    }
    s = Stack(f"agentlens-{secrets.token_hex(4)}", port, env)
    try:
        yield s
    finally:
        docker("compose", "--project-name", s.project, "-f", str(COMPOSE), "down", "-v",
               check=False, env=env)  # fmt: skip


def test_compose_file_publishes_on_loopback_only_req204a(stack: Stack) -> None:
    service = json.loads(stack.compose("config", "--format", "json"))["services"]["agentlens"]
    assert service["environment"]["AGENTLENS_AUTH_MODE"] == "none"  # explicit, never implied
    assert {p["host_ip"] for p in service["ports"]} == {"127.0.0.1"}
    assert {(v["source"], v["target"]) for v in service["volumes"]} == {("agentlens-data", "/data")}
    assert service["read_only"] is True
    assert service["image"] == image()


def test_compose_up_to_a_visible_run_req204a_364(stack: Stack) -> None:
    started = time.monotonic()
    stack.compose("up", "-d", "--wait", "--wait-timeout", "120")
    container = json.loads(stack.compose("ps", "--format", "json").splitlines()[0])["Name"]
    base = f"http://127.0.0.1:{stack.port}"
    assert httpx.get(f"{base}/", timeout=10).status_code == 200
    key = docker("exec", container, "cat", "/data/bootstrap-api-key").strip()
    send_run(base, key, "compose")
    assert run_agents(container) == ["compose"]
    print(f"compose: up to a visible Run in {time.monotonic() - started:.1f}s")  # noqa: T201
