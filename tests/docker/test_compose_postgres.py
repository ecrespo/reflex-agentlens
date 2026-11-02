"""``compose.postgres.yaml``, the production topology (plan task P7-04.2; REQ-205, REQ-364,
REQ-072.b, Tech Design DD-021 T3).

The stack runs with Caddy's internal CA (``AGENTLENS_TLS=internal``) and two collectors. Every
span batch is delivered twice, once over OTLP/HTTP and once over OTLP/gRPC, both through Caddy
and spread over the collectors; the database must end up with each span exactly once.
"""

from __future__ import annotations

import json
import secrets
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import grpc  # type: ignore[import-untyped]
import httpx
import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from tests.contract.ingest_rig import now_request
from tests.docker.conftest import ROOT, docker, image
from tests.integration.live import free_port
from tests.unit.ingest import otlp

pytestmark = pytest.mark.docker

COMPOSE = ROOT / "compose.postgres.yaml"
RUNS = 30


@dataclass
class Stack:
    project: str
    https: int
    grpc: int
    http: int
    env: dict[str, str] = field(default_factory=dict)

    def compose(self, *args: str, check: bool = True) -> str:
        return docker("compose", "--project-name", self.project, "-f", str(COMPOSE), *args,
                      check=check, env=self.env)  # fmt: skip

    def exec(self, service: str, *args: str) -> str:
        return self.compose("exec", "-T", service, *args)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    https, grpc_port, http = free_port(), free_port(), free_port()
    env = {
        "AGENTLENS_IMAGE": image(),
        "AGENTLENS_DOMAIN": "localhost",
        "AGENTLENS_TLS": "internal",
        "AGENTLENS_PUBLIC_URL": f"https://localhost:{https}",
        "AGENTLENS_HTTP_PORT": str(http),
        "AGENTLENS_HTTPS_PORT": str(https),
        "AGENTLENS_GRPC_PORT": str(grpc_port),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(16),
        "AGENTLENS_ADMIN_PASSWORD": secrets.token_urlsafe(16),
    }
    s = Stack(f"agentlens-pg-{secrets.token_hex(4)}", https, grpc_port, http, env)
    try:
        s.compose("up", "-d", "--wait", "--wait-timeout", "240", "--scale", "collector=2")
        yield s
    finally:
        s.compose("down", "-v", check=False)


def test_only_caddy_publishes_ports_req205b(stack: Stack) -> None:
    spec = json.loads(stack.compose("config", "--format", "json"))
    published = {name for name, svc in spec["services"].items() if svc.get("ports")}
    assert published == {"caddy"}
    assert spec["networks"]["internal"]["internal"] is True
    for name in ("postgres", "app", "collector"):
        assert set(spec["services"][name]["networks"]) == {"internal"}
    assert spec["services"]["app"]["environment"]["AGENTLENS_SERVER_INGEST_ENABLED"] == "false"
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text()
    for header in ("X-Forwarded-User", "X-Forwarded-Groups"):  # no forged identities (proxy mode)
        assert f"header_up -{header}" in caddyfile


def test_every_service_is_healthy_with_two_collectors_req205a_c(stack: Stack) -> None:
    rows = [json.loads(line) for line in stack.compose("ps", "--format", "json").splitlines()]
    services = sorted(row["Service"] for row in rows)
    assert services == ["app", "caddy", "collector", "collector", "postgres"]
    assert all("(healthy)" in row["Status"] for row in rows), rows


def _ca(stack: Stack, tmp: Path) -> Path:
    path = tmp / "caddy-root.crt"
    path.write_text(stack.exec("caddy", "cat", "/data/caddy/pki/authorities/local/root.crt"))
    return path


def _batch(i: int) -> ExportTraceServiceRequest:
    """One Run of two spans."""
    trace = 0x5000 + i
    root = otlp.span(trace=trace, sid=1, name="invoke_agent pg", start_ago_s=5, end_ago_s=1,
                     gen_ai__operation__name="invoke_agent", gen_ai__agent__name="pg")  # fmt: skip
    child = otlp.span(trace=trace, sid=2, parent=1, name="chat m", start_ago_s=4, end_ago_s=2,
                      gen_ai__operation__name="chat", gen_ai__request__model="m")  # fmt: skip
    return now_request(root, child)


def _counts(stack: Stack) -> tuple[int, int]:
    out = stack.exec(
        "postgres", "psql", "-U", "agentlens", "-d", "agentlens", "-Atc",
        "SELECT count(*) || ',' || count(DISTINCT (run_pk, span_id)) FROM spans",
    ).strip()  # fmt: skip
    total, distinct = out.split(",")
    return int(total), int(distinct)


def test_traffic_through_caddy_no_loss_no_duplicates_req205c_072b(
    stack: Stack, tmp_path: Path
) -> None:
    ca = _ca(stack, tmp_path)
    tls = ssl.create_default_context(cafile=str(ca))
    base = f"https://localhost:{stack.https}"
    with httpx.Client(verify=tls, timeout=15) as client:
        assert client.get(f"{base}/").status_code == 200  # UI through Caddy, TLS
        assert client.get(f"{base}/readyz").json()["status"] == "ready"  # a collector
        redirect = httpx.get(f"http://localhost:{stack.http}/runs", timeout=10)
        assert redirect.status_code == 308
        assert redirect.headers["location"] == "https://localhost/runs"

        key = stack.exec("app", "cat", "/data/bootstrap-api-key").strip()
        headers = {"content-type": "application/x-protobuf", "authorization": f"Bearer {key}"}
        credentials = grpc.ssl_channel_credentials(root_certificates=ca.read_bytes())
        channel = grpc.secure_channel(f"localhost:{stack.grpc}", credentials)
        stub = trace_service_pb2_grpc.TraceServiceStub(channel)
        try:
            for i in range(RUNS):
                request = _batch(i)
                body = request.SerializeToString()
                response = client.post(f"{base}/v1/traces", content=body, headers=headers)
                assert response.status_code == 200, response.text
                stub.Export(request, metadata=[("authorization", f"Bearer {key}")], timeout=15)
        finally:
            channel.close()

    deadline = time.monotonic() + 30
    while _counts(stack)[0] < 2 * RUNS and time.monotonic() < deadline:
        time.sleep(0.5)
    time.sleep(2)  # anything late would be a duplicate
    assert _counts(stack) == (2 * RUNS, 2 * RUNS)
    runs = stack.exec("postgres", "psql", "-U", "agentlens", "-d", "agentlens", "-Atc",
                      "SELECT count(*) FROM runs").strip()  # fmt: skip
    assert int(runs) == RUNS
