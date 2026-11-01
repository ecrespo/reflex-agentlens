"""Lifespan, health routes and first-run bootstrap (plan tasks P5-01.2, P5-02.1, P5-02.2).

REQ-340.a (health), REQ-074.a/b/c (schema at startup), REQ-060.b (pricing seed), REQ-201.a/b
(bootstrap), REQ-321.a (session secret), REQ-323.a (no key in logs), REQ-004.b (collect).
"""

from __future__ import annotations

import io
import logging
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from reflex_agentlens.app.bootstrap import KEY_FILE, SECRET_FILE, session_secret
from reflex_agentlens.app.lifespan import NEWER_HINT, SchemaMismatch, runtime
from reflex_agentlens.app.server import AppHandle, build_app
from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import ConfigError, StorageUnavailable
from reflex_agentlens.core.security import (
    SecretHash,
    generate_api_key,
    parse_api_key,
    verify_secret,
)
from tests.contract.ingest_rig import now_request
from tests.integration.app.conftest import Fresh
from tests.unit.ingest import otlp

PB = "application/x-protobuf"


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@asynccontextmanager
async def running(
    fresh: Fresh, cfg: AgentlensConfig, mode: str = "serve", banner: io.StringIO | None = None
) -> AsyncIterator[tuple[AppHandle, httpx.AsyncClient]]:
    handle = build_app(cfg, mode, banner=banner, runtime_options=fresh.runtime_options)  # type: ignore[arg-type]
    async with handle.app.router.lifespan_context(handle.app):
        transport = httpx.ASGITransport(app=handle.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield handle, client


async def key_works(handle: AppHandle, plaintext: str) -> bool:
    assert handle.runtime is not None
    key_id, secret = parse_api_key(plaintext)
    record = await handle.runtime.backend.resolve_api_key(key_id)
    return record is not None and verify_secret(secret, SecretHash(record.salt, record.secret_hash))


async def test_first_start_migrates_seeds_and_bootstraps_req_074a_201a(
    fresh: Fresh, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cfg = fresh.config()
    out = io.StringIO()  # not a TTY: the key goes to a 0600 file
    async with running(fresh, cfg, banner=out) as (handle, client):
        rt = handle.runtime
        assert rt is not None
        assert rt.engine is not None
        assert rt.engine.ready
        assert (await rt.backend.list_pricing_versions())[0].source == "bundled"  # REQ-060.b
        projects = await rt.backend.list_projects()
        assert [p.slug for p in projects] == ["default"]
        key_file = cfg.server.data_dir / KEY_FILE  # type: ignore[operator]
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        plaintext = key_file.read_text().strip()
        assert await key_works(handle, plaintext)
        assert f"API key written to {key_file} (mode 0600)" in out.getvalue()
        assert plaintext not in out.getvalue()
        assert plaintext not in caplog.text  # REQ-323.a

        assert (await client.get("/healthz")).json() == {"status": "ok"}
        ready = await client.get("/readyz")
        assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
        body = now_request(otlp.span(trace=1, sid=1)).SerializeToString()
        export = await client.post(
            "/v1/traces",
            content=body,
            headers={"content-type": PB, "authorization": f"Bearer {plaintext}"},
        )
        assert export.status_code == 200
    assert rt.engine.stopping  # leaving the lifespan stopped the engine

    again = io.StringIO()
    async with running(fresh, cfg, banner=again) as (handle, _):
        assert handle.runtime is not None
        assert len(await handle.runtime.backend.list_projects()) == 1
        assert len(await handle.runtime.backend.list_pricing_versions()) == 1
    assert again.getvalue().count("\n") == 1  # banner line only, nothing created


async def test_tty_banner_shows_the_key_once_req_201a(fresh: Fresh) -> None:
    cfg = fresh.config()
    out = Tty()
    async with running(fresh, cfg, banner=out) as (handle, _):
        text_out = out.getvalue()
        line = next(ln for ln in text_out.splitlines() if "API key (shown once)" in ln)
        plaintext = line.rsplit(" ", 1)[1]
        assert await key_works(handle, plaintext)
        assert "export OTEL_EXPORTER_OTLP_HEADERS" in text_out
    assert not (cfg.server.data_dir / KEY_FILE).exists()  # type: ignore[operator]


async def test_configured_bootstrap_key_is_hashed_and_never_printed_req_201b(
    fresh: Fresh, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    plaintext = generate_api_key().plaintext
    cfg = fresh.config(**{"bootstrap.api_key": plaintext})
    out = Tty()
    async with running(fresh, cfg, banner=out) as (handle, _):
        assert await key_works(handle, plaintext)
        assert handle.runtime is not None
        assert handle.runtime.bootstrap is not None
        assert plaintext not in repr(handle.runtime.bootstrap)
    assert plaintext not in out.getvalue()
    assert "configured bootstrap API key" in out.getvalue()
    assert plaintext not in caplog.text
    assert not (cfg.server.data_dir / KEY_FILE).exists()  # type: ignore[operator]


async def test_outdated_schema_without_auto_migrate_exits_with_the_hint_req_074b(
    fresh: Fresh,
) -> None:
    cfg = fresh.config(**{"database.auto_migrate": False})
    with pytest.raises(SchemaMismatch, match="reflex-agentlens db upgrade"):
        async with runtime(cfg, "serve", **fresh.runtime_options):
            pass


async def test_newer_schema_refuses_to_start_req_074c(fresh: Fresh) -> None:
    cfg = fresh.config()
    async with runtime(cfg, "serve", **fresh.runtime_options) as rt:
        stamp = text("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        async with rt.backend.write_engine.begin() as conn:
            await conn.execute(stamp)
    with pytest.raises(SchemaMismatch) as exc:
        async with runtime(cfg, "serve", **fresh.runtime_options):
            pass
    assert str(exc.value) == NEWER_HINT


async def test_unreachable_database_is_storage_unavailable(fresh: Fresh, tmp_path: Path) -> None:
    if fresh.name == "sqlite":
        blocker = tmp_path / "file"
        blocker.write_text("")
        cfg = fresh.config(**{"database.url": f"sqlite:///{blocker}/sub/agentlens.db"})
    else:
        cfg = fresh.config(**{"database.url": "postgresql://nobody:x@127.0.0.1:1/none"})
    with pytest.raises(StorageUnavailable):
        async with runtime(cfg, "serve", **fresh.runtime_options):
            pass


async def test_readyz_names_the_failing_check_req_340a(
    fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(fresh, fresh.config()) as (handle, client):
        rt = handle.runtime
        assert rt is not None
        assert (await client.get("/readyz")).status_code == 200  # schema verified once

        async def down() -> bool:
            return False

        monkeypatch.setattr(rt.backend, "ping", down)
        rt.health._at = -float("inf")  # drop the 1 s probe cache
        ready = await client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json() == {
            "status": "not_ready",
            "checks": {"database": "fail", "schema": "ok", "ingest": "ok"},
        }
        assert (await client.get("/healthz")).status_code == 200  # alive all the same
        rt.health.shutting_down = True
        gone = await client.get("/healthz")
        assert (gone.status_code, gone.json()) == (503, {"status": "shutting_down"})
        for response in (ready, gone):
            assert "version" not in response.text
            assert fresh.url not in response.text


async def test_collect_has_no_bootstrap_or_query_services_req_004b(fresh: Fresh) -> None:
    if fresh.name == "sqlite":
        with pytest.raises(ConfigError):
            async with runtime(fresh.config(), "collect", **fresh.runtime_options):
                pass
        return
    out = Tty()
    async with running(fresh, fresh.config(), "collect", banner=out) as (handle, client):
        rt = handle.runtime
        assert rt is not None
        assert (rt.bootstrap, rt.query, rt.admin) == (None, None, None)
        assert rt.engine is not None
        assert await rt.backend.list_projects() == []
        assert (await client.get("/readyz")).status_code == 200
    assert out.getvalue() == ""


def test_session_secret_file_is_private_and_reused_req_321(tmp_path: Path) -> None:
    fresh = Fresh("sqlite", tmp_path, f"sqlite:///{tmp_path / 'x.db'}")
    cfg = fresh.config()
    first = session_secret(cfg)
    path = cfg.server.data_dir / SECRET_FILE  # type: ignore[operator]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(first) == 64
    assert session_secret(cfg) == first
    configured = fresh.config(**{"auth.session_secret": "s" * 40})
    assert session_secret(configured) == b"s" * 40
    with pytest.raises(ConfigError):
        session_secret(fresh.config(**{"auth.session_secret": "short"}))
