"""Standalone auth: session cookie and viewers (plan task P5-04.1; REQ-224.a/e, 321.a, API §7.3)."""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from reflex_agentlens.app.auth import (
    LOCAL_VIEWER,
    cookie_value,
    read_session,
    session_cookie,
    standalone_viewer,
)
from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.core.security import sign

KEY = b"k" * 32
OTHER = b"o" * 32
NOW = 1_800_000_000


def config(tmp_path: Path, **overrides: object) -> AgentlensConfig:
    return load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path, cli_overrides=overrides)


@given(user=st.text(min_size=1, max_size=64), ttl=st.integers(1, 10**7))
def test_cookie_round_trips(user: str, ttl: int) -> None:
    session = read_session(KEY, session_cookie(KEY, user, NOW, ttl), NOW)
    assert session is not None
    assert (session.sub, session.iat, session.exp) == (user, NOW, NOW + ttl)


def test_forged_expired_and_foreign_cookies_are_refused_req_321a() -> None:
    good = session_cookie(KEY, "admin", NOW, 60)
    assert read_session(KEY, good, NOW + 59) is not None
    assert read_session(KEY, good, NOW + 60) is None  # expired
    assert read_session(OTHER, good, NOW) is None  # another instance's secret
    body, mac = good.split(".")
    assert read_session(KEY, f"{body}.{mac[:-2]}AA", NOW) is None  # tampered signature
    forged = sign(KEY, "cursor", b'{"sub":"admin","iat":1,"exp":9999999999}')
    assert read_session(KEY, forged, NOW) is None  # signed for another purpose
    for payload in (b"[]", b'{"sub":"","iat":1,"exp":9999999999}', b'{"sub":"a","iat":"1","exp":2}',
                    b'{"sub":"a","iat":true,"exp":9999999999}', b"not json"):  # fmt: skip
        assert read_session(KEY, sign(KEY, "session", payload), NOW) is None
    future = sign(KEY, "session", f'{{"sub":"a","iat":{NOW + 3600},"exp":{NOW + 7200}}}'.encode())
    assert read_session(KEY, future, NOW) is None  # issued in the future
    assert read_session(KEY, None, NOW) is None
    assert read_session(KEY, "garbage", NOW) is None


def test_cookie_header_parsing() -> None:
    assert cookie_value("a=1; agentlens_session=tok.mac; b=2") == "tok.mac"
    assert cookie_value('agentlens_session="tok.mac"') == "tok.mac"
    assert cookie_value("xagentlens_session=1") is None
    assert cookie_value("agentlens_session=") is None
    assert cookie_value(None) is None


def test_none_mode_is_the_local_admin_req_224c(tmp_path: Path) -> None:
    assert standalone_viewer(config(tmp_path), KEY, {}, None) == LOCAL_VIEWER


def test_password_mode_reads_the_cookie_req_224a(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"auth.mode": "password", "auth.admin_password": "pw-pw-pw-pw"})
    cookie = f"agentlens_session={session_cookie(KEY, 'admin', NOW, 60)}"
    viewer = standalone_viewer(cfg, KEY, {"Cookie": cookie}, "203.0.113.9", NOW)
    assert viewer is not None
    assert (viewer.id, viewer.is_admin, viewer.allowed_project_ids) == ("admin", True, None)
    assert standalone_viewer(cfg, KEY, {"cookie": cookie}, None, NOW + 61) is None
    assert standalone_viewer(cfg, KEY, {}, None, NOW) is None
    stranger = f"agentlens_session={session_cookie(KEY, 'mallory', NOW, 60)}"
    assert standalone_viewer(cfg, KEY, {"cookie": stranger}, None, NOW) is None


def test_proxy_headers_only_from_trusted_peers_req_224a(tmp_path: Path) -> None:
    cfg = config(tmp_path, **{"auth.mode": "proxy", "auth.trusted_proxies": "10.0.0.0/8, ::1/128"})
    headers = {"X-Forwarded-User": "ana\x07 ", "X-Forwarded-Groups": "dev"}
    viewer = standalone_viewer(cfg, KEY, headers, "10.1.2.3")
    assert viewer is not None
    assert (viewer.id, viewer.is_admin) == ("ana", True)  # no admin group configured
    assert standalone_viewer(cfg, KEY, headers, "::ffff:10.1.2.3") is not None  # IPv4-mapped
    assert standalone_viewer(cfg, KEY, headers, "::1") is not None
    assert standalone_viewer(cfg, KEY, headers, "192.0.2.1") is None  # untrusted peer
    assert standalone_viewer(cfg, KEY, headers, None) is None
    assert standalone_viewer(cfg, KEY, headers, "not-an-ip") is None
    assert standalone_viewer(cfg, KEY, {"X-Forwarded-User": "  "}, "10.0.0.1") is None
    long_user = {"x-forwarded-user": "u" * 1000}
    assert len(standalone_viewer(cfg, KEY, long_user, "10.0.0.1").id) == 256  # type: ignore[union-attr]


def test_proxy_admin_group_gates_admin_rights_req_224e(tmp_path: Path) -> None:
    cfg = config(
        tmp_path,
        **{
            "auth.mode": "proxy",
            "auth.trusted_proxies": "10.0.0.0/8",
            "auth.proxy_admin_group": "ops",
        },
    )
    admin = standalone_viewer(
        cfg, KEY, {"x-forwarded-user": "a", "x-forwarded-groups": "dev, ops"}, "10.0.0.1"
    )
    plain = standalone_viewer(
        cfg, KEY, {"x-forwarded-user": "b", "x-forwarded-groups": "dev"}, "10.0.0.1"
    )
    assert admin is not None
    assert admin.is_admin
    assert plain is not None
    assert not plain.is_admin
    none = standalone_viewer(cfg, KEY, {"x-forwarded-user": "c"}, "10.0.0.1")
    assert none is not None
    assert not none.is_admin
