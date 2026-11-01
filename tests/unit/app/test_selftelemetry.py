"""Self-telemetry: default off, loop check, missing extra (plan task P5-07.1; REQ-342.b/c)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from reflex_agentlens.app import selftelemetry
from reflex_agentlens.app.selftelemetry import EXTRA_HINT, check_loop, start
from reflex_agentlens.cli.main import main
from reflex_agentlens.core import telemetry
from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.core.errors import ConfigError

HOSTS = {
    "localhost": ["127.0.0.1", "::1"],
    "collector.example": ["203.0.113.5"],
    "box.internal": ["10.0.0.5"],
    "mapped.example": ["::ffff:127.0.0.1"],
}


def resolve(host: str, _: int) -> list[str]:
    return HOSTS.get(host, [])


def config(tmp_path: Path, **overrides: object) -> AgentlensConfig:
    base: dict[str, object] = {"server.data_dir": str(tmp_path / "data")}
    base.update(overrides)
    return load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path, cli_overrides=base)


def enabled(tmp_path: Path, endpoint: str, **overrides: object) -> AgentlensConfig:
    return config(
        tmp_path,
        **{"self_telemetry.enabled": True, "self_telemetry.endpoint": endpoint, **overrides},
    )


def test_disabled_by_default_req_342c(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    assert cfg.self_telemetry.enabled is False
    assert start(cfg) is None
    assert type(telemetry.recorder()) is telemetry.Recorder  # the no-op one


@pytest.mark.parametrize(
    ("endpoint", "overrides", "loops"),
    [
        ("http://127.0.0.1:8000", {}, True),  # our HTTP port on loopback
        ("http://localhost:8000/", {}, True),
        ("http://[::1]:8000", {}, True),
        ("http://0.0.0.0:8000", {}, True),  # unspecified
        ("http://mapped.example:8000", {}, True),  # IPv4-mapped loopback
        ("http://localhost:4317", {"server.grpc_enabled": True}, True),  # our gRPC port
        ("http://localhost:4317", {"server.grpc_enabled": False}, False),  # gRPC not listening
        ("http://localhost:4318", {}, False),  # another collector on this machine
        ("http://localhost", {}, False),  # port 80
        ("http://collector.example:8000", {}, False),  # a remote host on the same port number
        ("http://box.internal:8000", {"server.http_host": "10.0.0.5"}, True),  # bind address
        ("http://box.internal:8000", {"server.http_host": "box.internal"}, True),
        ("http://box.internal:9000", {"server.http_host": "10.0.0.5", "server.http_port": 9000},
         True),
        ("https://collector.example:443", {"server.http_port": 443}, False),
        ("http://unresolvable.example:8000", {}, False),  # fails at export, never loops
    ],
)  # fmt: skip
def test_loop_check_table_req_342b(
    tmp_path: Path, endpoint: str, overrides: dict[str, object], loops: bool
) -> None:
    cfg = enabled(tmp_path, endpoint, **overrides)
    if loops:
        with pytest.raises(ConfigError) as info:
            check_loop(cfg, resolve)
        assert info.value.key == "self_telemetry.endpoint"
        assert "self-loop" in str(info.value)
    else:
        check_loop(cfg, resolve)


def test_missing_extra_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.metrics", None)  # import fails
    with pytest.raises(ConfigError) as info:
        start(enabled(tmp_path, "http://collector.example:4318"), resolve)
    assert "[selftelemetry]" in str(info.value)
    assert EXTRA_HINT in str(info.value)
    assert type(telemetry.recorder()) is telemetry.Recorder  # nothing installed


def test_cli_exits_2_on_a_self_loop_req_342b(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    env = {
        "HOME": str(tmp_path),
        "AGENTLENS_SERVER_DATA_DIR": str(tmp_path / "data"),
        "AGENTLENS_SELF_TELEMETRY_ENABLED": "true",
        "AGENTLENS_SELF_TELEMETRY_ENDPOINT": "http://127.0.0.1:8000",
    }
    code = main(["serve"], env=env, stdout=out, stderr=err)
    assert code == 2
    assert "self_telemetry.endpoint" in err.getvalue()


def test_start_installs_and_shutdown_restores(tmp_path: Path) -> None:
    handle = start(enabled(tmp_path, "http://collector.example:4318"), resolve)
    assert handle is not None
    try:
        assert isinstance(telemetry.recorder(), selftelemetry.OtelRecorder)
    finally:
        handle.shutdown()
    assert type(telemetry.recorder()) is telemetry.Recorder
