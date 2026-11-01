"""Standalone app, shell, login page and build rxconfig (plan task P6-11.1; REQ-192, 224,
REQ-324, DD-002)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import reflex as rx
from reflex_base.registry import RegistrationContext

from reflex_agentlens._webapp.build import APP_MODULE, frontend_dir, rxconfig_source
from reflex_agentlens.app.auth import COOKIE, LOCAL_VIEWER, session_cookie
from reflex_agentlens.app.login import LOGIN_PATH, LOGOUT_PATH
from reflex_agentlens.app.standalone import (
    LoginState,
    StandaloneAuth,
    build_standalone,
    login_route_for,
    standalone_app,
)
from reflex_agentlens.components.shell import ShellState, nav_links
from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.views import registry as reg_module

PASSWORD = "correct horse battery"  # noqa: S105 - test value


def config(tmp_path: Path, **overrides: object) -> AgentlensConfig:
    base: dict[str, object] = {"auth.mode": "none", "server.data_dir": str(tmp_path / "data")}
    base.update(overrides)
    return load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path, cli_overrides=base)


@pytest.fixture(autouse=True)
def isolated() -> Iterator[None]:
    reg_module.reset()
    token = RegistrationContext.set(RegistrationContext.ensure_context().fork())
    try:
        yield
    finally:
        RegistrationContext.reset(token)
        reg_module.reset()


def compiled(app: rx.App) -> dict[str, str]:
    from reflex.compiler import compiler

    out = {}
    for route in list(app._unevaluated_pages):
        app._compile_page(route)
        out[route] = compiler._compile_page(app._pages[route], route)
    return out


def test_standalone_app_compiles_with_shell_and_login_req_192a() -> None:
    app = standalone_app()
    js = compiled(app)
    assert set(js) == {
        "index", "runs", "trace/[trace_id]", "sessions", "sessions/[session_id]", "settings",
        "login",
    }  # fmt: skip
    for route, code in js.items():
        if route != "login":
            assert "agentlens-shell" in code, route  # every exported page, same shell
    login = js["login"]
    assert f'action:"{LOGIN_PATH}"' in login
    assert 'method:"post"' in login
    assert 'name:"password"' in login
    assert "handleSubmit" not in login  # a native form: the server sets the cookie
    assert f'action:"{LOGOUT_PATH}"' in js["runs"]
    reg = reg_module.registry()
    assert (reg.prefix, reg.nav) == ("/", True)
    assert any(getattr(t, "__name__", "") == "agentlens_lifespan" for t in app._lifespan_tasks)


def test_serve_builds_it_without_a_lifespan(tmp_path: Path) -> None:
    app = build_standalone(config(tmp_path), lifespan=False)
    assert not any(getattr(t, "__name__", "") == "agentlens_lifespan" for t in app._lifespan_tasks)
    assert reg_module.registry().config is not None


def test_login_route_follows_the_auth_mode_req_191b(tmp_path: Path) -> None:
    assert login_route_for(config(tmp_path)) is None
    password = config(tmp_path, **{"auth.mode": "password", "auth.admin_password": PASSWORD})
    assert login_route_for(password) == "/login"
    build_standalone(password, lifespan=False)
    assert reg_module.registry().login_route == "/login"


def test_nav_links_follow_prefix_and_pages() -> None:
    every = frozenset({"dashboard", "runs", "trace", "sessions", "session", "settings"})
    assert nav_links("/", every) == [
        ("Dashboard", "/"), ("Runs", "/runs"), ("Sessions", "/sessions"),
        ("Settings", "/settings"),
    ]  # fmt: skip
    assert nav_links("/obs", frozenset({"runs", "trace"})) == [("Runs", "/obs/runs")]


def state(headers: dict[str, str], ip: str = "127.0.0.1", url: str = "/") -> Any:
    return SimpleNamespace(
        router=SimpleNamespace(
            headers=SimpleNamespace(raw_headers=headers),
            session=SimpleNamespace(client_ip=ip),
            url=url,
        )
    )


async def test_standalone_auth_none_mode_api_7_3(tmp_path: Path) -> None:
    reg_module.registry().config = config(tmp_path)
    assert await StandaloneAuth()(state({})) == LOCAL_VIEWER
    assert not (tmp_path / "data" / "session_secret").exists()  # no key needed, none made


async def test_standalone_auth_password_mode_reads_the_cookie_req_224a(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"auth.mode": "password", "auth.admin_password": PASSWORD,
                              "auth.session_secret": "s" * 32})  # fmt: skip
    reg_module.registry().config = cfg
    hook = StandaloneAuth()
    assert await hook(state({})) is None
    fresh = session_cookie(b"s" * 32, "admin", int(time.time()), 3600)
    viewer = await hook(state({"cookie": f"other=1; {COOKIE}={fresh}"}))
    assert viewer is not None
    assert (viewer.id, viewer.is_admin) == ("admin", True)
    forged = session_cookie(b"x" * 32, "admin", int(time.time()), 3600)
    assert await hook(state({"cookie": f"{COOKIE}={forged}"})) is None


async def test_standalone_auth_proxy_mode_trusts_listed_peers_only(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"auth.mode": "proxy", "auth.trusted_proxies": ["10.0.0.0/8"]})
    reg_module.registry().config = cfg
    hook, header = StandaloneAuth(), {cfg.auth.proxy_user_header: "ana"}
    assert await hook(state(header, ip="203.0.113.9")) is None
    viewer = await hook(state(header, ip="10.1.2.3"))
    assert viewer is not None
    assert viewer.id == "ana"


@pytest.mark.parametrize(
    ("url", "login_route", "next_path", "failed", "redirects"),
    [
        ("http://h/login?next=/runs%3Fproject%3Dp&error=1", "/login", "/runs?project=p", True,
         False),
        ("http://h/login?next=//evil.example", "/login", "/", False, False),
        ("http://h/login?next=/runs", None, "/runs", False, True),
    ],
)  # fmt: skip
def test_login_page_state(
    monkeypatch: pytest.MonkeyPatch, url: str, login_route: str | None, next_path: str,
    failed: bool, redirects: bool,
) -> None:  # fmt: skip
    reg_module.registry().login_route = login_route
    monkeypatch.setattr(LoginState, "router", SimpleNamespace(url=url), raising=False)
    login = LoginState()
    result = login.load_login()  # type: ignore[operator]
    assert (login.next_path, login.failed) == (next_path, failed)
    assert (result is not None) is redirects


def test_shell_sign_out_only_with_a_login_route() -> None:
    shell = ShellState()
    shell.load_shell()  # type: ignore[operator]
    assert shell.sign_out is False
    reg_module.registry().login_route = "/login"
    shell.load_shell()  # type: ignore[operator]
    assert shell.sign_out is True


def test_rxconfig_turns_telemetry_and_sitemap_off_req_324() -> None:
    namespace: dict[str, Any] = {}
    exec(compile(rxconfig_source(), "rxconfig.py", "exec"), namespace)  # noqa: S102 - our text
    cfg = namespace["config"]
    assert cfg.telemetry_enabled is False
    assert cfg.app_module_import == APP_MODULE
    assert cfg.show_built_with_reflex is False
    assert not any(isinstance(p, rx.plugins.SitemapPlugin) for p in cfg.plugins)
    assert any(isinstance(p, rx.plugins.RadixThemesPlugin) for p in cfg.plugins)
    assert tuple(cfg.cors_allowed_origins) == ("*",)  # build only; runtime pins it
    pinned: dict[str, Any] = {}
    exec(compile(rxconfig_source(cors_origin="http://127.0.0.1:9123"), "rx", "exec"), pinned)  # noqa: S102
    assert list(pinned["config"].cors_allowed_origins) == ["http://127.0.0.1:9123"]


def test_frontend_dir_is_inside_the_package() -> None:
    assert frontend_dir().parent.name == "reflex_agentlens"
    assert frontend_dir().name == "_frontend"
