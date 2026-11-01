"""Process lifespan: schema, pricing seed, bootstrap and the ingest engine (plan task P5-02.1).

Startup order (Tasks P5 design notes; REQ-074, 060.b, 201, DD-001):

1. open the serving-loop backend (``StorageUnavailable`` → exit 5);
2. schema: OUTDATED + ``auto_migrate`` → migrate; OUTDATED otherwise or NEWER →
   :class:`SchemaMismatch` (exit 4) with the command or the message REQ-074.b/c ask for;
3. bundled pricing seed on an empty table (``ingest.cost.bootstrap_pricing``);
4. ``serve`` only: first-run bootstrap and the banner (``app.bootstrap``);
5. the ingest engine, when this process ingests;
6. ready. Leaving the context stops the engine (drain, REQ-331) and closes the backend.

Before step 1, opt-in self-telemetry (``app.selftelemetry``) runs its loop check and installs
its exporter; it is flushed and removed last.

The ingest engine opens its own backend on the ingest loop (asyncpg connections are
loop-bound). It shares the serving backend's :class:`ChangeFeed` (thread-safe) and origin ID, so
the UI sees ingest commits in-process and PostgreSQL NOTIFY echoes of this process are skipped;
only the serving backend runs the PostgreSQL listener.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TextIO

from reflex_agentlens.app import selftelemetry
from reflex_agentlens.app.backend import Backend, Mode, check_mode, make_backend, open_backend
from reflex_agentlens.app.bootstrap import BootstrapResult, announce, first_run, session_secret
from reflex_agentlens.app.routes import Health
from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import AgentlensError
from reflex_agentlens.ingest.core import build_engine
from reflex_agentlens.ingest.cost import bootstrap_pricing, load_bundled_pricing
from reflex_agentlens.ingest.engine import EngineStore, IngestEngine, Service
from reflex_agentlens.query.admin import AdminService
from reflex_agentlens.query.service import QueryService
from reflex_agentlens.store.records import SchemaStatus

__all__ = ["Runtime", "SchemaMismatch", "ingests", "prepare_schema", "runtime"]

UPGRADE_HINT = "the database schema is outdated; run `reflex-agentlens db upgrade`"
NEWER_HINT = (
    "the database schema is newer than this agentlens: downgrade the database or upgrade agentlens"
)


class SchemaMismatch(AgentlensError):
    """Schema outdated with auto-migrate off, or newer than the code (exit 4, REQ-074.b/c)."""


@dataclass(slots=True)
class Runtime:
    """What a running process owns; the server and the CLI read it."""

    cfg: AgentlensConfig
    mode: Mode
    backend: Backend  # serving loop
    health: Health
    engine: IngestEngine | None
    query: QueryService | None  # serve / demo
    admin: AdminService | None
    bootstrap: BootstrapResult | None


async def prepare_schema(backend: Backend, cfg: AgentlensConfig) -> None:
    status = await backend.schema_status()
    if status is SchemaStatus.OUTDATED:
        if not cfg.database.auto_migrate:
            raise SchemaMismatch(UPGRADE_HINT)
        await backend.migrate()  # PostgreSQL: one process migrates, the others wait (074.d)
    elif status is SchemaStatus.NEWER:
        raise SchemaMismatch(NEWER_HINT)


def ingests(cfg: AgentlensConfig, mode: Mode) -> bool:
    """Whether this process runs the ingest engine (and so the OTLP routes)."""
    return mode in ("serve", "collect") and cfg.server.ingest_enabled and not cfg.demo.enabled


@asynccontextmanager
async def runtime(
    cfg: AgentlensConfig,
    mode: Mode,
    *,
    banner: TextIO | None = None,
    backend_options: dict[str, Any] | None = None,
    extra_services: list[Service] | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
) -> AsyncIterator[Runtime]:
    """Run the startup order above; ``banner`` receives the first-run banner (``serve``)."""
    check_mode(cfg, mode)
    options = backend_options or {}
    tracing = selftelemetry.start(cfg)  # loop check first: a bad endpoint is exit 2
    try:
        backend = await open_backend(cfg, listen=True, **options)
    except BaseException:
        if tracing is not None:
            tracing.shutdown()
        raise
    try:
        await prepare_schema(backend, cfg)
        await bootstrap_pricing(backend, load_bundled_pricing(cfg.pricing.file))
        boot: BootstrapResult | None = None
        if mode == "serve" and not cfg.demo.enabled:
            boot = await first_run(backend, cfg, clock_ns=clock_ns)
            if banner is not None:
                announce(cfg, boot, banner)

        engine: IngestEngine | None = None
        if ingests(cfg, mode):

            async def open_store() -> EngineStore:
                store = make_backend(cfg, **options)
                store.changes = backend.changes  # one feed per process (thread-safe)
                store.origin_id = backend.origin_id
                await store.open()
                return store

            engine = build_engine(cfg, open_store, extra_services=extra_services)

        query = admin = None
        if mode in ("serve", "demo"):
            query = QueryService(
                backend,
                session_secret(cfg),
                clock_ns=clock_ns,
                size_limit_bytes=cfg.retention.storage_size_limit,
            )
            admin = AdminService(backend, cfg, clock_ns=clock_ns)
        health = Health(backend, None if engine is None else (lambda: engine.ready))
        rt = Runtime(cfg, mode, backend, health, engine, query, admin, boot)
        if engine is not None:
            await engine.start()
        try:
            yield rt
        finally:
            health.shutting_down = True
            if engine is not None:
                await engine.stop()
    finally:
        await backend.close()
        if tracing is not None:
            tracing.shutdown()
