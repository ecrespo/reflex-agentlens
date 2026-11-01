"""Login/logout routes, throttle and CORS (plan task P5-04.2; REQ-224.a/d, 321.a, 325, API §4)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from reflex_agentlens.app.auth import COOKIE, read_session
from reflex_agentlens.app.login import (
    LOGIN_PATH,
    LOGOUT_PATH,
    LoginThrottle,
    cors_origin,
    login_routes,
    safe_next,
)
from reflex_agentlens.app.server import build_app
from reflex_agentlens.core.config import AgentlensConfig, load_config

KEY = b"k" * 32
PASSWORD = "correct horse battery"  # noqa: S105 - test value
WRONG = "bad"
HOST = "agentlens.test"
FORM = "application/x-www-form-urlencoded"


def config(tmp_path: Path, **overrides: object) -> AgentlensConfig:
    base: dict[str, object] = {
        "auth.mode": "password",
        "auth.admin_password": PASSWORD,
        "server.data_dir": str(tmp_path / "data"),
    }
    base.update(overrides)
    return load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path, cli_overrides=base)


class Rig:
    def __init__(self, cfg: AgentlensConfig, scheme: str = "http") -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.throttle = LoginThrottle(clock=lambda: self.now)

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)

        routes = login_routes(cfg, KEY, throttle=self.throttle, now_s=lambda: 1_000, sleep=sleep)
        self.app = Starlette(routes=routes)
        self.base = f"{scheme}://{HOST}"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("198.51.100.7", 1234)),
            base_url=self.base,
        )

    async def login(
        self, client: httpx.AsyncClient, password: str = PASSWORD, **extra: str
    ) -> httpx.Response:
        origin = extra.pop("origin", self.base)
        data = {"username": "admin", "password": password, **extra}
        return await client.post(
            LOGIN_PATH, data=data, headers={"origin": origin, "content-type": FORM}
        )


async def test_valid_login_sets_the_session_cookie_req_224a_321a(tmp_path: Path) -> None:
    rig = Rig(config(tmp_path))
    async with rig.client() as client:
        response = await rig.login(client, next="/agentlens/runs?status=error")
    assert response.status_code == 303
    assert response.headers["location"] == "/agentlens/runs?status=error"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for flag in ("HttpOnly", "Path=/", "SameSite=lax", "Max-Age=43200"):
        assert flag in cookie
    assert "Secure" not in cookie  # plain HTTP
    token = cookie.split(";")[0].split("=", 1)[1]
    session = read_session(KEY, token, 1_000)
    assert session is not None
    assert (session.sub, session.exp) == ("admin", 1_000 + 43_200)


async def test_https_public_url_marks_the_cookie_secure_req_321a(tmp_path: Path) -> None:
    rig = Rig(config(tmp_path, **{"server.public_url": f"https://{HOST}"}), scheme="https")
    async with rig.client() as client:
        response = await rig.login(client)
    assert "Secure" in response.headers["set-cookie"]


async def test_invalid_credentials_give_no_hint(tmp_path: Path) -> None:
    rig = Rig(config(tmp_path))
    async with rig.client() as client:
        wrong_password = await rig.login(client, password=WRONG)
        wrong_user = await client.post(
            LOGIN_PATH,
            data={"username": "root", "password": PASSWORD},
            headers={"origin": rig.base, "content-type": FORM},
        )
    for response in (wrong_password, wrong_user):
        assert response.status_code == 303
        assert response.headers["location"] == "/login?error=1"
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"referer": "https://evil.example/agentlens/login"},
        {"origin": "null"},
        {},
    ],
)
async def test_cross_origin_login_is_forbidden(tmp_path: Path, headers: dict[str, str]) -> None:
    rig = Rig(config(tmp_path))
    async with rig.client() as client:
        response = await client.post(
            LOGIN_PATH,
            data={"username": "admin", "password": PASSWORD},
            headers={"content-type": FORM, **headers},
        )
        assert response.status_code == 403
        logout = await client.post(LOGOUT_PATH, headers=headers)
        assert logout.status_code == 403


async def test_referer_from_the_same_host_or_public_url_is_accepted(tmp_path: Path) -> None:
    rig = Rig(config(tmp_path, **{"server.public_url": "https://public.example"}))
    async with rig.client() as client:
        for referer in (f"{rig.base}/login", "https://public.example/login"):
            response = await client.post(
                LOGIN_PATH,
                data={"username": "admin", "password": PASSWORD},
                headers={"referer": referer, "content-type": FORM},
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    "target",
    ["https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)",
     "agentlens/runs", "", "/ok\x00", "http:/evil"],
)  # fmt: skip
def test_open_redirects_fall_back_to_root(target: str) -> None:
    assert safe_next(target) == "/"


def test_relative_paths_are_kept() -> None:
    assert safe_next("/agentlens/traces/abc?tab=raw#x") == "/agentlens/traces/abc?tab=raw#x"


async def test_throttle_after_five_failures_req_224d(tmp_path: Path) -> None:
    rig = Rig(config(tmp_path))
    async with rig.client() as client:
        for _ in range(5):
            await rig.login(client, password=WRONG)
        assert rig.slept == []
        await rig.login(client, password=WRONG)
        await rig.login(client)  # even a correct attempt waits once throttled
        assert rig.slept == [1.0, 1.0]
        rig.now += 61  # the window slides
        await rig.login(client, password=WRONG)
        assert rig.slept == [1.0, 1.0]
        await rig.login(client)  # success clears the client's failures
        rig.slept.clear()
        for _ in range(5):
            await rig.login(client, password=WRONG)
        assert rig.slept == []


def test_throttle_memory_is_bounded() -> None:
    throttle = LoginThrottle(max_clients=3)
    for ip in ("a", "b", "c", "d"):
        for _ in range(5):
            throttle.failed(ip)
    assert throttle.delay("a") == 0.0  # evicted
    assert throttle.delay("d") == 1.0


async def test_logout_clears_the_cookie(tmp_path: Path) -> None:
    rig = Rig(config(tmp_path))
    async with rig.client() as client:
        response = await client.post(LOGOUT_PATH, headers={"origin": rig.base})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f'{COOKIE}=""') or cookie.startswith(f"{COOKIE}=;")
    assert "Max-Age=0" in cookie


async def test_oversized_form_is_refused(tmp_path: Path) -> None:
    rig = Rig(config(tmp_path))
    async with rig.client() as client:
        response = await rig.login(client, filler="x" * 10_000)
    assert response.status_code == 413


@pytest.mark.parametrize("mode", ["none", "proxy"])
async def test_routes_exist_only_in_password_mode(tmp_path: Path, mode: str) -> None:
    assert login_routes(config(tmp_path, **{"auth.mode": mode}), KEY) == []


def test_cors_origin(tmp_path: Path) -> None:
    assert cors_origin(config(tmp_path)) == "http://127.0.0.1:8000"
    public = config(tmp_path, **{"server.public_url": "https://a.example:8443/agentlens/"})
    assert cors_origin(public) == "https://a.example:8443"
    v6 = config(tmp_path, **{"server.http_host": "::1"})
    assert cors_origin(v6) == "http://[::1]:8000"


async def test_cors_never_allows_a_foreign_origin_req_325(tmp_path: Path) -> None:
    handle = build_app(config(tmp_path, **{"auth.mode": "none"}), "serve")
    transport = httpx.ASGITransport(app=handle.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        preflight = {"access-control-request-method": "POST"}
        foreign = await client.options(
            "/v1/traces", headers={"origin": "https://evil.example", **preflight}
        )
        assert foreign.status_code == 400
        assert "access-control-allow-origin" not in foreign.headers
        own = await client.options(
            "/v1/traces", headers={"origin": "http://127.0.0.1:8000", **preflight}
        )
        assert own.headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
        assert own.headers.get("access-control-allow-origin") != "*"
