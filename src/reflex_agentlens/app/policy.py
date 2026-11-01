"""Bind policy for the standalone UI (plan task P5-01.1; REQ-224.b/c, Tech Design §6.1).

A UI reachable from the network must have an explicit ``auth.mode``. On a loopback bind an unset
mode means ``none``. An explicit ``none`` on a non-loopback bind is allowed (``compose.yaml``
publishes the port on ``127.0.0.1`` only) but logged as a warning. ``collect`` serves no UI, so
the policy applies to ``serve`` and ``demo`` only; ingest is protected by API keys either way.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Literal

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import ConfigError

__all__ = ["AuthMode", "check_bind", "effective_auth_mode", "is_loopback"]

log = logging.getLogger("agentlens.app")

AuthMode = Literal["none", "password", "proxy"]

_HELP = (
    "the UI binds to a non-loopback address; set auth.mode explicitly: "
    "'password' (login with auth.admin_password), 'proxy' (trust auth.proxy_user_header from "
    "auth.trusted_proxies) or 'none' (only behind a port published on 127.0.0.1)"
)


def is_loopback(host: str) -> bool:
    """``localhost`` and loopback IPs; anything else (hostnames included) counts as exposed."""
    name = host.strip().strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def check_bind(cfg: AgentlensConfig) -> None:
    """REQ-224.b: refuse a non-loopback HTTP bind (the UI port) without ``auth.mode`` (exit 2);
    REQ-224.c: warn about an explicit ``none`` on one. The gRPC port carries ingest only."""
    if is_loopback(cfg.server.http_host):
        return
    if cfg.auth.mode is None:
        raise ConfigError("auth.mode", "effective configuration", _HELP)
    if cfg.auth.mode == "none":
        log.warning("auth.mode is none on a non-loopback bind: the UI has no login")


def effective_auth_mode(cfg: AgentlensConfig) -> AuthMode:
    """The mode in force once :func:`check_bind` passed: unset means ``none``."""
    return cfg.auth.mode or "none"
