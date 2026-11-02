"""Serving the pre-built frontend: workdir, port rewrite, CSP, mounting (plan task P6-11.2;
REQ-193.a, REQ-200.b, DD-002, DD-018)."""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from reflex_agentlens.app import webserve
from reflex_agentlens.app.server import build_app
from reflex_agentlens.app.webserve import (
    NOT_BUILT,
    CspMiddleware,
    Ui,
    asgi_lifespan,
    bundle_dir,
    csp_policy,
    inline_script_hashes,
    load_ui,
    prepare_workdir,
    reflex_env,
)
from reflex_agentlens.core.config import AgentlensConfig, load_config

ENV_MODULE = (
    "var e={PING:`http://localhost:8000/ping`,EVENT:`ws://localhost:8000/_event`,"
    "UPLOAD:`http://localhost:8000/_upload`,TRANSPORT:`websocket`};export{e as t};"
)
INLINE = "window.__x = 1;"
PAGE = f'<html><head><script>{INLINE}</script><script src="/a.js"></script></head></html>'


def config(tmp_path: Path, **overrides: object) -> AgentlensConfig:
    base: dict[str, object] = {"auth.mode": "none", "server.data_dir": str(tmp_path / "data")}
    base.update(overrides)
    return load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path, cli_overrides=base)


def fake_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "runs").mkdir()
    (bundle / "index.html").write_text(PAGE)
    (bundle / "runs" / "index.html").write_text("<script>other()</script>")
    env = bundle / "assets" / "reflex-env-abc.js"
    env.write_text(ENV_MODULE)
    env.with_name(env.name + ".gz").write_bytes(gzip.compress(ENV_MODULE.encode()))
    env.with_name(env.name + ".br").write_bytes(b"stale")
    (bundle / ".gitignore").write_text("*\n")
    return bundle


def test_bundle_dir_needs_an_index(tmp_path: Path) -> None:
    assert bundle_dir(tmp_path) is None
    assert bundle_dir(fake_bundle(tmp_path)) == tmp_path / "bundle"


def test_prepare_workdir_rewrites_the_port_dd002(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"server.http_port": 9123})
    wd = prepare_workdir(cfg, fake_bundle(tmp_path), version="9.9.9")
    assert wd.root == tmp_path / "data" / "web" / "9.9.9"
    assert wd.client == wd.root / ".web" / "build" / "client"
    env = wd.client / "assets" / "reflex-env-abc.js"
    text = env.read_text()
    assert wd.rewritten == 3
    assert "localhost:8000" not in text
    assert "`ws://localhost:9123/_event`" in text
    assert "`http://localhost:9123/ping`" in text
    # the precompressed sibling is regenerated (the static handler prefers it), others removed
    assert gzip.decompress(env.with_name(env.name + ".gz").read_bytes()).decode() == text
    assert not env.with_name(env.name + ".br").exists()
    assert not (wd.client / ".gitignore").exists()
    # hashes come from the shipped HTML, every page, scripts with src skipped (DD-018)
    assert wd.script_hashes == tuple(
        sorted(inline_script_hashes(PAGE) + inline_script_hashes("<script>other()</script>"))
    )
    rxconfig = (wd.root / "rxconfig.py").read_text()
    assert "telemetry_enabled=False" in rxconfig
    assert "cors_allowed_origins=['http://127.0.0.1:9123']" in rxconfig


def test_prepare_workdir_prefers_the_shipped_csp_json_dd018(tmp_path: Path) -> None:
    bundle = fake_bundle(tmp_path)
    shipped = "'sha256-c2hpcHBlZA=='"
    (bundle / "csp.json").write_text(json.dumps({"script_hashes": [shipped]}))
    wd = prepare_workdir(config(tmp_path), bundle, version="1")
    assert wd.script_hashes == (shipped,)
    assert not (wd.client / "csp.json").exists()  # not served as a static file


def test_prepare_workdir_ignores_a_malformed_csp_json(tmp_path: Path) -> None:
    bundle = fake_bundle(tmp_path)
    (bundle / "csp.json").write_text(json.dumps({"script_hashes": ["'unsafe-inline'"]}))
    wd = prepare_workdir(config(tmp_path), bundle, version="1")
    assert wd.script_hashes == tuple(
        sorted(inline_script_hashes(PAGE) + inline_script_hashes("<script>other()</script>"))
    )


def test_prepare_workdir_is_repeatable_and_follows_public_url(tmp_path: Path) -> None:
    bundle = fake_bundle(tmp_path)
    prepare_workdir(config(tmp_path), bundle, version="1")
    cfg = config(tmp_path, **{"server.public_url": "https://obs.example.com/"})
    wd = prepare_workdir(cfg, bundle, version="1")
    text = (wd.client / "assets" / "reflex-env-abc.js").read_text()
    assert "`wss://obs.example.com/_event`" in text
    assert "`https://obs.example.com/ping`" in text


def test_reflex_env_dd002() -> None:
    wd = webserve.Workdir(Path("/d/web/1"), Path("/d/web/1/.web/build/client"), (), 0)
    env = reflex_env(wd)
    assert env["REFLEX_WEB_WORKDIR"] == "/d/web/1/.web"
    assert env["REFLEX_STATES_WORKDIR"] == "/d/web/1/.states"
    assert env["REFLEX_ENV_MODE"] == "prod"
    assert env["REFLEX_TELEMETRY_ENABLED"] == "false"
    assert env["__REFLEX_MOUNT_FRONTEND_COMPILED_APP"] == "true"
    assert env["__REFLEX_SKIP_COMPILE"] == "true"


def test_csp_policy_req_193a() -> None:
    policy = csp_policy(["'sha256-b'", "'sha256-a'", "'sha256-a'"], "ws://h:1", "'self'")
    assert "script-src 'self' 'sha256-a' 'sha256-b';" in policy
    assert "connect-src 'self' ws://h:1;" in policy
    assert "unsafe-eval" not in policy
    assert "'unsafe-inline'" not in policy.split("style-src")[0]  # scripts: hashes only
    for directive in ("object-src 'none'", "base-uri 'self'", "form-action 'self'",
                      "frame-ancestors 'self'", "default-src 'self'"):  # fmt: skip
        assert directive in policy
    sources = [w for w in policy.replace(";", " ").split() if "://" in w]
    assert sources == ["ws://h:1"]  # no third-party origin
    assert "connect-src 'self';" in csp_policy([], None, "'none'")


def _app() -> Starlette:
    return Starlette(routes=[Route("/", lambda _: PlainTextResponse("ui"))])


async def _get(app: object, host: str, scheme: str = "http") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url=f"{scheme}://{host}") as client:
        return await client.get("/")


@pytest.mark.parametrize(
    ("host", "scheme", "connect"),
    [
        ("127.0.0.1:9123", "http", "connect-src 'self' ws://127.0.0.1:9123;"),
        ("obs.example", "https", "connect-src 'self' wss://obs.example;"),
        ("[::1]:8000", "http", "connect-src 'self' ws://[::1]:8000;"),
    ],
)
async def test_csp_middleware_derives_the_ws_origin(
    tmp_path: Path, host: str, scheme: str, connect: str
) -> None:
    app = CspMiddleware(_app(), config(tmp_path), ["'sha256-x'"])
    response = await _get(app, host, scheme)
    assert response.text == "ui"
    assert connect in response.headers["content-security-policy"]


async def test_csp_middleware_ignores_a_forged_host(tmp_path: Path) -> None:
    app = CspMiddleware(_app(), config(tmp_path), [])
    response = await _get(app, "evil;script-src *")
    policy = response.headers["content-security-policy"]
    assert "connect-src 'self';" in policy
    assert "evil" not in policy


async def test_csp_middleware_prefers_public_url(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"server.public_url": "https://obs.example.com"})
    response = await _get(CspMiddleware(_app(), cfg, []), "127.0.0.1:8000")
    policy = response.headers["content-security-policy"]
    assert "connect-src 'self' wss://obs.example.com;" in policy


async def test_serve_mounts_the_ui_after_its_routes_req_193a(tmp_path: Path) -> None:
    handle = build_app(config(tmp_path), "serve", ui=Ui(_app(), ("'sha256-x'",)))
    page = await _get(handle.app, "127.0.0.1:8000")
    assert (page.status_code, page.text) == (200, "ui")
    assert "'sha256-x'" in page.headers["content-security-policy"]
    transport = httpx.ASGITransport(app=handle.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        health = await c.get("/healthz")  # the operational route wins over the mount
    assert health.status_code == 503  # no runtime: not started, never the UI


async def test_collect_never_mounts_a_ui(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"database.url": "postgresql+asyncpg://u:p@localhost/db"})
    handle = build_app(cfg, "collect", ui=Ui(_app()))
    assert (await _get(handle.app, "127.0.0.1:4318")).status_code == 404


async def test_without_a_build_serve_explains_how_req_200b(tmp_path: Path) -> None:
    banner = io.StringIO()
    ui = load_ui(config(tmp_path), "serve", bundle=tmp_path / "missing", banner=banner)
    assert ui == Ui(None)
    assert "reflex_agentlens._webapp.build" in banner.getvalue()
    handle = build_app(config(tmp_path), "serve", ui=ui)
    response = await _get(handle.app, "127.0.0.1:8000")
    assert (response.status_code, response.text) == (503, NOT_BUILT)
    assert "content-security-policy" not in response.headers


async def test_asgi_lifespan_runs_the_mounted_apps_lifespan() -> None:
    seen: list[str] = []

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        seen.append("start")
        yield
        seen.append("stop")

    async with asgi_lifespan(Starlette(lifespan=lifespan)):
        assert seen == ["start"]
    assert seen == ["start", "stop"]


async def test_asgi_lifespan_reports_a_failed_startup() -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        await boom()
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="startup"):
        async with asgi_lifespan(Starlette(lifespan=lifespan)):
            pass  # pragma: no cover
