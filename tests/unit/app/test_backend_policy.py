"""Backend factory and bind policy (plan task P5-01.1; REQ-004.b, 071, 072, 224.b/c)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from reflex_agentlens.app.backend import backend_kind, check_mode, make_backend
from reflex_agentlens.app.policy import check_bind, effective_auth_mode, is_loopback
from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.core.errors import ConfigError
from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.sqlite.backend import SqliteBackend

PG = "postgresql://u:p@db.example:5432/agentlens"


def config(tmp_path: Path, **overrides: object) -> AgentlensConfig:
    return load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path, cli_overrides=overrides)


def test_default_url_is_sqlite_under_the_data_dir_req_071(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"server.data_dir": str(tmp_path / "d")})
    backend = make_backend(cfg)
    assert isinstance(backend, SqliteBackend)
    assert backend.path == tmp_path / "d" / "agentlens.db"
    assert backend_kind(cfg) == "sqlite"


def test_postgres_url_builds_a_postgres_backend_req_072(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"database.url": PG, "database.pool_size": 3})
    backend = make_backend(cfg, search_path="s1")
    assert isinstance(backend, PostgresBackend)
    assert backend.pool_size == 3
    assert backend.search_path == "s1"
    assert backend.finalization_window_ns == 300 * 10**9  # [ingest] finalization_window = 5m
    assert backend_kind(config(tmp_path, **{"database.url": "postgres://x/y"})) == "postgres"


def test_collect_refuses_sqlite_req_004b(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        check_mode(config(tmp_path), "collect")
    assert exc.value.key == "database.url"
    assert "PostgreSQL" in exc.value.reason
    check_mode(config(tmp_path), "serve")
    check_mode(config(tmp_path, **{"database.url": PG}), "collect")


def test_in_memory_sqlite_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        make_backend(config(tmp_path, **{"database.url": "sqlite:///:memory:"}))


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.0.0.53", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("app.localhost", True),
        ("0.0.0.0", False),  # noqa: S104 - the case under test
        ("::", False),
        ("192.168.1.10", False),
        ("example.org", False),
        ("", False),
    ],
)
def test_loopback_detection(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


def test_non_loopback_bind_without_auth_mode_is_refused_req_224b(tmp_path: Path) -> None:
    exposed = config(tmp_path, **{"server.http_host": "0.0.0.0"})  # noqa: S104
    with pytest.raises(ConfigError) as exc:
        check_bind(exposed)
    assert exc.value.key == "auth.mode"
    for option in ("password", "proxy", "none"):
        assert option in exc.value.reason
    check_bind(config(tmp_path))  # loopback: implicit none
    assert effective_auth_mode(config(tmp_path)) == "none"
    grpc_only = config(tmp_path, **{"server.grpc_host": "0.0.0.0"})  # noqa: S104
    check_bind(grpc_only)  # the gRPC port carries ingest only


def test_explicit_none_on_non_loopback_warns_req_224c(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    cfg = config(tmp_path, **{"server.http_host": "0.0.0.0", "auth.mode": "none"})  # noqa: S104
    check_bind(cfg)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    caplog.clear()
    secured = {"server.http_host": "0.0.0.0", "auth.mode": "proxy"}  # noqa: S104
    check_bind(config(tmp_path, **secured))
    assert caplog.records == []
    assert effective_auth_mode(config(tmp_path, **secured)) == "proxy"
