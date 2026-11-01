"""Storage backend from the configuration (plan task P5-01.1; REQ-004.b, 071, 072).

``database.url`` picks the backend: ``sqlite://`` → :class:`SqliteBackend` (one file, one
writing process), ``postgresql://`` → :class:`PostgresBackend`. ``collect`` scales out against
PostgreSQL only, so it refuses a SQLite URL (PRD §12 Q4; API §8.3 → exit 2).
"""

from __future__ import annotations

from typing import Any, Literal

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import ConfigError, StorageUnavailable
from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.sqlite.backend import SqliteBackend

__all__ = [
    "Backend",
    "BackendKind",
    "Mode",
    "backend_kind",
    "check_mode",
    "make_backend",
    "open_backend",
]

Mode = Literal["serve", "collect", "demo", "admin"]  # "admin": one-shot CLI commands
BackendKind = Literal["sqlite", "postgres"]
Backend = SqliteBackend | PostgresBackend

_CONFIG = "effective configuration"


def _url(cfg: AgentlensConfig) -> str:
    url = cfg.database.url
    if url is None:  # load_config always derives one; a hand-built config may not
        raise ConfigError("database.url", _CONFIG, "not set")
    return url.get_secret_value()


def backend_kind(cfg: AgentlensConfig) -> BackendKind:
    scheme = _url(cfg).split("://", 1)[0].split("+", 1)[0]
    return "sqlite" if scheme == "sqlite" else "postgres"


def check_mode(cfg: AgentlensConfig, mode: Mode) -> None:
    """``collect`` needs PostgreSQL (exit 2 otherwise)."""
    if mode == "collect" and backend_kind(cfg) == "sqlite":
        raise ConfigError(
            "database.url",
            _CONFIG,
            "collect needs a PostgreSQL database URL; use serve for a SQLite database",
        )


def make_backend(cfg: AgentlensConfig, **overrides: Any) -> Backend:
    """A closed backend for ``cfg``; ``overrides`` reach the constructor (tests: search path)."""
    window = int(cfg.ingest.finalization_window.total_seconds() * 1_000_000_000)
    kwargs: dict[str, Any] = {"finalization_window_ns": window, **overrides}
    url = _url(cfg)
    if backend_kind(cfg) == "sqlite":
        try:
            return SqliteBackend.from_url(url, **kwargs)
        except ValueError as exc:
            raise ConfigError("database.url", _CONFIG, str(exc)) from None
    return PostgresBackend(url, **{"pool_size": cfg.database.pool_size, **kwargs})


async def open_backend(cfg: AgentlensConfig, *, listen: bool = False, **overrides: Any) -> Backend:
    """Open the backend; ``listen`` also starts the PostgreSQL change listener (DD-007).

    Raises ``StorageUnavailable`` when the database cannot be reached (exit 5 at startup).
    """
    backend = make_backend(cfg, **overrides)
    try:
        await backend.open()
    except OSError as exc:  # SQLite: the directory cannot be created or opened
        raise StorageUnavailable("cannot open the database") from exc
    if listen and isinstance(backend, PostgresBackend):
        try:
            await backend.start_change_listener()
        except BaseException:
            await backend.close()
            raise
    return backend
