"""reflex-agentlens: self-hosted observability for AI agents, built with Reflex.

Public API (API §7.1, semver-stable from 0.1.0): ``mount``, ``configure``, ``AgentlensConfig``,
``Viewer``, ``AuthHook``, ``pages`` (``standalone_app`` arrives with P6-11). Every symbol is
resolved lazily, so ``import reflex_agentlens`` imports neither Reflex nor the storage layer and
performs no I/O (REQ-190.c).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0.dev0"

__all__ = [
    "AgentlensConfig",
    "AuthHook",
    "Viewer",
    "__version__",
    "configure",
    "mount",
    "pages",
]

if TYPE_CHECKING:
    from reflex_agentlens import pages
    from reflex_agentlens.app.mount import mount
    from reflex_agentlens.core.config import AgentlensConfig
    from reflex_agentlens.query.dto import Viewer
    from reflex_agentlens.views.registry import AuthHook, configure

_LAZY: dict[str, tuple[str, str | None]] = {
    "mount": ("reflex_agentlens.app.mount", "mount"),
    "configure": ("reflex_agentlens.views.registry", "configure"),
    "AuthHook": ("reflex_agentlens.views.registry", "AuthHook"),
    "AgentlensConfig": ("reflex_agentlens.core.config", "AgentlensConfig"),
    "Viewer": ("reflex_agentlens.query.dto", "Viewer"),
    "pages": ("reflex_agentlens.pages", None),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'reflex_agentlens' has no attribute {name!r}")
    module = importlib.import_module(target[0])
    value = module if target[1] is None else getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
