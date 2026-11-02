"""The container image (plan task P7-02.2; REQ-203, REQ-326, REQ-364, Tech Design DD-021)."""

from __future__ import annotations

import json
import secrets
import warnings
from collections.abc import Iterator

import httpx
import pytest

from tests.docker.conftest import docker, image, run_agents, send_run, wait_healthy
from tests.integration.live import free_port

pytestmark = pytest.mark.docker

SIZE_TARGET = 500 * 1024 * 1024  # REQ-203.b (SHOULD)
PG_IMAGE = "postgres:18.6-trixie"
READYZ = (
    "import urllib.request as u; print(u.urlopen('http://127.0.0.1:4318/readyz', timeout=2).status)"
)
TMPFS = "/tmp"  # noqa: S108 - the container's tmpfs mount point, not a host temp file


@pytest.fixture
def container() -> Iterator[tuple[str, int]]:
    """``serve`` under the hardened profile: read-only root, tmpfs /tmp, a volume on /data."""
    name, port = f"agentlens-test-{secrets.token_hex(4)}", free_port()
    docker(
        "run", "-d", "--name", name, "--read-only", "--tmpfs", TMPFS,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{name}:/data", "-e", "AGENTLENS_AUTH_MODE=none",
        "-e", f"AGENTLENS_SERVER_HTTP_PORT={port}", "-p", f"127.0.0.1:{port}:{port}",
        image(),
    )  # fmt: skip
    try:
        yield name, port
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", name, check=False)


def test_image_metadata_req203a_326a() -> None:
    config = json.loads(docker("image", "inspect", "-f", "{{json .Config}}", image()))
    assert config["User"] == "10001:10001"
    assert {"8000/tcp", "4317/tcp"} <= set(config["ExposedPorts"])
    assert "/data" in config["Volumes"]
    assert config["Healthcheck"]["Test"][0] == "CMD"
    assert config["Entrypoint"] == ["reflex-agentlens"]
    assert config["Cmd"] == ["serve"]


def test_image_has_no_toolchain_and_ships_the_frontend_req203b() -> None:
    script = (
        "import shutil, reflex_agentlens._webapp.build as b\n"
        "print([t for t in ('node', 'bun', 'gcc', 'cc', 'npm', 'uv', 'pip') if shutil.which(t)])\n"
        "f = b.frontend_dir()\n"
        "print((f / 'index.html').is_file(), (f / 'csp.json').is_file())"
    )
    out = docker("run", "--rm", "--entrypoint", "python", image(), "-c", script).splitlines()
    assert out == ["[]", "True True"]
    size = int(docker("image", "inspect", "-f", "{{.Size}}", image()).strip())
    if size > SIZE_TARGET:
        warnings.warn(f"image is {size / 2**20:.0f} MiB (> 500 MiB target)", stacklevel=1)


def test_serve_is_healthy_read_only_and_ingests_req203_326_364(
    container: tuple[str, int],
) -> None:
    name, port = container
    wait_healthy(name, 90)
    base = f"http://127.0.0.1:{port}"
    page = httpx.get(f"{base}/", timeout=10)
    assert page.status_code == 200
    assert "'sha256-" in page.headers["content-security-policy"]
    key = docker("exec", name, "cat", "/data/bootstrap-api-key").strip()
    send_run(base, key, "docker-image")
    assert run_agents(name) == ["docker-image"]
    assert docker("exec", name, "id", "-u").strip() == "10001"


def test_same_image_runs_collect_req203c() -> None:
    """``collect`` needs PostgreSQL, so this runs one on a private network."""
    name = f"agentlens-collect-{secrets.token_hex(4)}"
    db = f"{name}-pg"
    docker("network", "create", name)
    try:
        docker("run", "-d", "--name", db, "--network", name, "-e", "POSTGRES_PASSWORD=agentlens",
               "--health-cmd", "pg_isready -U postgres", "--health-interval", "1s",
               PG_IMAGE)  # fmt: skip
        wait_healthy(db, 60)
        docker("run", "-d", "--name", name, "--network", name, "--read-only", "--tmpfs", TMPFS,
               "-e", f"AGENTLENS_DATABASE_URL=postgresql+asyncpg://postgres:agentlens@{db}/postgres",
               image(), "collect")  # fmt: skip
        wait_healthy(name, 90)  # the healthcheck finds collect on 4318
        status = docker("exec", name, "python", "-c", READYZ).strip()
        assert status == "200"
    finally:
        docker("rm", "-f", name, db, check=False)
        docker("network", "rm", name, check=False)
