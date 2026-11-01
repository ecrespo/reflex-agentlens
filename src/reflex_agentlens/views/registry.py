"""Per-process registry of the embedding (plan tasks P6-01.2, P6-03.1; API §7.1).

``configure()`` and ``mount()`` write here; pages read it. No I/O and no Reflex import, so
``import reflex_agentlens`` stays free of side effects (REQ-190.c). The services live in the
``app.lifespan.Runtime`` that the app's lifespan task registers once storage is open.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.guard import ViewerCache

if TYPE_CHECKING:
    from reflex_agentlens.app.lifespan import Runtime
    from reflex_agentlens.query.admin import AdminService
    from reflex_agentlens.query.service import QueryService

__all__ = ["AuthHook", "Registry", "configure", "registry", "services"]

AuthHook = Callable[[Any], Awaitable[Viewer | None]]


@dataclass
class Registry:
    config: AgentlensConfig | None = None
    auth: AuthHook | None = None
    login_route: str | None = None
    prefix: str = "/agentlens"
    demo: bool = False
    nav: bool = True
    pages: frozenset[str] = frozenset()
    runtime: Runtime | None = None
    viewers: ViewerCache = field(default_factory=ViewerCache)


_REGISTRY = Registry()


def registry() -> Registry:
    return _REGISTRY


def configure(config: AgentlensConfig | None = None) -> None:
    """API §7.1: register the config for this process. No I/O."""
    _REGISTRY.config = config


def reset() -> None:
    """Tests only: forget every registration."""
    global _REGISTRY
    _REGISTRY = Registry()


def services() -> tuple[QueryService, AdminService]:
    """The running services; ``StorageUnavailable`` until the lifespan has opened storage."""
    rt = _REGISTRY.runtime
    if rt is None or rt.query is None or rt.admin is None:
        raise StorageUnavailable("agentlens is starting")
    return rt.query, rt.admin
