"""Standalone UI auth: session cookie and viewer resolution (plan task P5-04.1).

API §7.3, REQ-224.a/e, REQ-321.a. Three modes:

- ``none``: every request is the local admin.
- ``password``: the ``agentlens_session`` cookie, ``<payload>.<hmac>`` signed with the session
  secret (``core.security.sign``, purpose ``session``), payload ``{"sub", "iat", "exp"}``. Only a
  cookie for the configured admin user and not expired yields a viewer.
- ``proxy``: the user header is trusted only when the peer address is in ``auth.trusted_proxies``;
  with ``auth.proxy_admin_group`` set, admin rights need that group in the groups header.

:func:`standalone_viewer` is pure (headers, peer address, time in), so the Reflex hook of P6-03
and the tests call the same code.
"""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass

from reflex_agentlens.app.policy import effective_auth_mode
from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.security import sign, unsign
from reflex_agentlens.query import dto

__all__ = [
    "COOKIE",
    "LOCAL_VIEWER",
    "Session",
    "cookie_value",
    "read_session",
    "session_cookie",
    "standalone_viewer",
]

COOKIE = "agentlens_session"
PURPOSE = "session"
MAX_USER_CHARS = 256
LOCAL_VIEWER = dto.Viewer("local", "local", is_admin=True)


@dataclass(frozen=True, slots=True)
class Session:
    sub: str
    iat: int
    exp: int


def session_cookie(key: bytes, user: str, now_s: int, ttl_s: int) -> str:
    """The signed cookie value for ``user``, valid for ``ttl_s`` seconds."""
    payload = {"sub": user, "iat": now_s, "exp": now_s + ttl_s}
    return sign(key, PURPOSE, json.dumps(payload, separators=(",", ":")).encode())


def read_session(key: bytes, token: str | None, now_s: int) -> Session | None:
    """``None`` for a missing, forged, malformed or expired cookie (never raises)."""
    if not token:
        return None
    try:
        data = json.loads(unsign(key, PURPOSE, token))
    except (ValidationError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sub, iat, exp = data.get("sub"), data.get("iat"), data.get("exp")
    if not isinstance(sub, str) or not sub:
        return None
    if type(iat) is not int or type(exp) is not int or exp <= now_s or iat > now_s + 60:
        return None
    return Session(sub, iat, exp)


def cookie_value(header: str | None, name: str = COOKIE) -> str | None:
    """The value of cookie ``name`` in a ``Cookie`` header (first occurrence)."""
    if not header:
        return None
    for part in header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value.strip().strip('"') or None
    return None


def _trusted(cfg: AgentlensConfig, client_ip: str | None) -> bool:
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip.strip().strip("[]"))
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in cfg.auth.trusted_proxies)


def _clean(value: str | None) -> str:
    """A header value as a display name: printable, trimmed, bounded."""
    if value is None:
        return ""
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:MAX_USER_CHARS]


def standalone_viewer(
    cfg: AgentlensConfig,
    key: bytes,
    headers: Mapping[str, str],
    client_ip: str | None,
    now_s: int | None = None,
) -> dto.Viewer | None:
    """API §7.3: the viewer of one request, or ``None`` (→ login page / 401)."""
    mode = effective_auth_mode(cfg)
    if mode == "none":
        return LOCAL_VIEWER
    lower = {k.lower(): v for k, v in headers.items()}
    if mode == "password":
        now = int(time.time()) if now_s is None else now_s
        session = read_session(key, cookie_value(lower.get("cookie")), now)
        if session is None or session.sub != cfg.auth.admin_username:
            return None
        return dto.Viewer(session.sub, session.sub, is_admin=True)
    if not _trusted(cfg, client_ip):
        return None
    user = _clean(lower.get(cfg.auth.proxy_user_header.lower()))
    if not user:
        return None
    group = cfg.auth.proxy_admin_group
    groups = lower.get(cfg.auth.proxy_groups_header.lower(), "")
    is_admin = group is None or group in {g.strip() for g in groups.split(",")}
    return dto.Viewer(user, user, is_admin=is_admin)
