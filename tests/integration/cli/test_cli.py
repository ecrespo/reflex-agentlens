"""The ``reflex-agentlens`` command in-process (plan tasks P5-03.1, 03.2, 03.3).

REQ-200.a (commands), REQ-202.a/b/d (configuration), REQ-074 (db), REQ-170.b (keys),
REQ-092.a (purge), REQ-042.a (rebuild), API §8.3 exit codes. Each test runs ``main(argv, env=…)``
against a fresh database (SQLite file or PostgreSQL database).
"""

from __future__ import annotations

import asyncio
import io
import socket
from pathlib import Path

import pytest
from sqlalchemy import text

from reflex_agentlens import __version__
from reflex_agentlens.app.backend import Backend, open_backend
from reflex_agentlens.app.lifespan import prepare_schema
from reflex_agentlens.cli.main import EXIT, build_parser, main
from reflex_agentlens.core.config import load_config
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.semconv.mapping import MAPPING_VERSION
from reflex_agentlens.store.records import NewProject, NormalizedBatch, ProjectPatch
from tests.contract.dataset import dataset
from tests.integration.app.conftest import Fresh

RUNS = dataset(21, 300)


def run(fresh: Fresh, *argv: str, **env: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env=fresh.env(**env), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


async def backend_for(fresh: Fresh) -> Backend:
    cfg = load_config(env=fresh.env(), cwd=fresh.tmp_path)
    backend = await open_backend(cfg)
    await prepare_schema(backend, cfg)
    return backend


async def project_with_runs(fresh: Fresh, slug: str, max_age_s: int | None = None) -> str:
    backend = await backend_for(fresh)
    try:
        project = await backend.create_project(NewProject(new_ulid(), slug, slug))
        if max_age_s is not None:
            await backend.update_project(project.id, ProjectPatch(retention_max_age_s=max_age_s))
        spans = tuple(sp for r in RUNS for sp in r)
        await backend.write_batch(NormalizedBatch(project.id, spans))
        return project.id
    finally:
        await backend.close()


def test_version_and_help_req_200a(fresh: Fresh) -> None:
    code, out, _ = run(fresh, "version")
    assert code == EXIT["ok"]
    assert out.splitlines()[0] == f"reflex-agentlens {__version__}"
    assert out.splitlines()[1].startswith("OTLP proto 1.")
    assert f"semconv mapping {MAPPING_VERSION}" in out
    parser = build_parser()
    commands = [
        ["--help"], ["serve", "--help"], ["collect", "--help"], ["demo", "--help"],
        ["db", "upgrade", "--help"], ["db", "status", "--help"], ["rebuild", "--help"],
        ["purge", "--help"], ["keys", "create", "--help"], ["keys", "list", "--help"],
        ["keys", "revoke", "--help"], ["version", "--help"],
    ]  # fmt: skip
    for argv in commands:
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(argv)
        assert exc.value.code == 0
    assert main(["nonsense"], env=fresh.env(), stdout=io.StringIO()) == EXIT["usage"]
    assert main(["keys"], env=fresh.env(), stdout=io.StringIO()) == EXIT["usage"]


def test_config_errors_name_key_and_source_req_202b(fresh: Fresh, tmp_path: Path) -> None:
    code, _, err = run(fresh, "db", "status", AGENTLENS_SERVER_NOPE="1")
    assert code == EXIT["usage"]
    assert "AGENTLENS_SERVER_NOPE" in err
    toml = tmp_path / "bad.toml"
    toml.write_text('[ingest]\nmax_body_bytes = "lots"\n')
    code, _, err = run(fresh, "--config", str(toml), "db", "status")
    assert code == EXIT["usage"]
    assert "ingest.max_body_bytes" in err
    assert str(toml) in err


def test_print_config_masks_secrets_req_202d(fresh: Fresh) -> None:
    secret = "hunter2-hunter2-hunter2"  # noqa: S105 - test value
    code, out, _ = run(
        fresh, "serve", "--port", "9123", "--print-config", AGENTLENS_AUTH_ADMIN_PASSWORD=secret
    )
    assert code == EXIT["ok"]
    assert "http_port = 9123" in out
    assert secret not in out
    assert fresh.url not in out
    assert '"***"' in out
    code, out, _ = run(fresh, "collect", "--print-config")
    assert "http_port = 4318" in out  # collect's own default (API §8.2)


def test_db_upgrade_and_status_req_074(fresh: Fresh) -> None:
    code, out, _ = run(fresh, "db", "status")
    assert (code, out.splitlines()[0]) == (EXIT["ok"], "OUTDATED")
    assert "database revision: none" in out
    code, out, _ = run(
        fresh, "keys", "list", "--project", "x", AGENTLENS_DATABASE_AUTO_MIGRATE="false"
    )
    assert code == EXIT["schema"]
    code, out, _ = run(fresh, "db", "upgrade")
    assert code == EXIT["ok"]
    code, out, _ = run(fresh, "db", "status")
    assert out.splitlines()[0] == "CURRENT"

    async def stamp() -> None:
        backend = await backend_for(fresh)
        async with backend.write_engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
        await backend.close()

    asyncio.run(stamp())
    code, out, _ = run(fresh, "db", "status")
    assert out.splitlines()[0] == "NEWER"
    code, _, err = run(fresh, "db", "upgrade")
    assert code == EXIT["schema"]
    assert "upgrade agentlens" in err


def test_keys_create_list_revoke_req_170b(fresh: Fresh) -> None:
    code, _, err = run(fresh, "keys", "create", "--project", "missing")
    assert code == EXIT["usage"]
    assert "not found" in err
    asyncio.run(project_with_runs(fresh, "shop"))
    code, out, _ = run(fresh, "keys", "create", "--project", "shop", "--label", "ci")
    assert code == EXIT["ok"]
    plaintext = next(ln for ln in out.splitlines() if "(shown once)" in ln).rsplit(" ", 1)[1]
    key_id = out.splitlines()[0].removeprefix("key_id: ")
    assert plaintext.startswith(f"al_{key_id}_")
    code, out, _ = run(fresh, "keys", "list", "--project", "shop")
    assert code == EXIT["ok"]
    assert key_id in out
    assert plaintext not in out
    assert "active" in out
    assert run(fresh, "keys", "revoke", key_id)[0] == EXIT["ok"]
    assert "revoked" in run(fresh, "keys", "list", "--project", "shop")[1]
    assert run(fresh, "keys", "revoke", "zzzzzzzz")[0] == EXIT["usage"]


def test_purge_dry_run_then_purge_req_092a(fresh: Fresh) -> None:
    asyncio.run(project_with_runs(fresh, "old-runs", max_age_s=1))  # every run is past its age
    code, out, _ = run(fresh, "purge", "--dry-run")
    assert code == EXIT["ok"]
    assert out.startswith(f"would delete {len(RUNS)} traces")
    assert run(fresh, "purge", "--dry-run")[1] == out  # nothing was deleted
    code, out, _ = run(fresh, "purge")
    assert code == EXIT["ok"]
    assert out.startswith(f"deleted {len(RUNS)} traces by age and 0 by size")

    async def last() -> str | None:
        backend = await backend_for(fresh)
        try:
            report = await backend.last_purge()
            return None if report is None else report.trigger
        finally:
            await backend.close()

    assert asyncio.run(last()) == "manual"
    assert run(fresh, "purge", "--dry-run")[1].startswith("would delete 0 traces")


def test_rebuild_scope_and_errors_req_042a(fresh: Fresh) -> None:
    asyncio.run(project_with_runs(fresh, "proj-a"))
    asyncio.run(project_with_runs(fresh, "proj-b"))
    code, out, _ = run(fresh, "rebuild")
    assert code == EXIT["ok"]
    assert f"Runs scanned: {2 * len(RUNS)}" in out
    code, out, _ = run(fresh, "rebuild", "--project", "proj-a", "--since", "2026-10-15T00:00:00Z")
    assert code == EXIT["ok"]
    scanned = int(out.split("Runs scanned: ")[1].split(",")[0])
    assert 0 < scanned < len(RUNS)
    assert run(fresh, "rebuild", "--project", "nope")[0] == EXIT["usage"]
    assert run(fresh, "rebuild", "--since", "yesterday")[0] == EXIT["usage"]


def test_unreachable_database_exits_5(fresh: Fresh, tmp_path: Path) -> None:
    if fresh.name == "sqlite":
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        url = f"sqlite:///{blocker}/x/agentlens.db"
    else:
        url = "postgresql://nobody:x@127.0.0.1:1/none"
    code, _, err = run(fresh, "db", "status", AGENTLENS_DATABASE_URL=url)
    assert code == EXIT["database"]
    assert "x@" not in err  # no credentials in the message
    assert run(fresh, "keys", "list", "--project", "p", AGENTLENS_DATABASE_URL=url)[0] == 5


def test_server_commands_refuse_bad_setups_before_binding(fresh: Fresh) -> None:
    if fresh.name == "sqlite":
        code, _, err = run(fresh, "collect")
        assert code == EXIT["usage"]  # REQ-004.b: collect needs PostgreSQL
        assert "database.url" in err
    code, _, err = run(fresh, "serve", "--host", "0.0.0.0")  # noqa: S104 - the case under test
    assert code == EXIT["usage"]  # REQ-224.b
    assert "auth.mode" in err
    code, _, err = run(fresh, "demo")
    assert code == EXIT["usage"]
    assert "P8-02" in err


def test_port_in_use_names_port_and_setting_req_200c(fresh: Fresh) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code, _, err = run(fresh, "serve", "--port", str(port))
    assert code == EXIT["port"]
    assert str(port) in err
    assert "server.http_port" in err
    assert "--port" in err
