"""Test-only ASGI app: the ingest engine started by the lifespan, as in ``serve`` (DD-001).

Run by Granian in a separate process (``--factory tests.integration.ingest_app:create_app``).
Environment: ``AGENTLENS_IT_CONFIG`` (TOML path), ``AGENTLENS_IT_DB`` (``sqlite:///path`` or a
PostgreSQL URL) and ``AGENTLENS_IT_SCHEMA`` (PostgreSQL search path).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from reflex_agentlens.core.config import load_config
from reflex_agentlens.ingest.core import build_engine
from reflex_agentlens.ingest.engine import EngineStore
from reflex_agentlens.ingest.http import otlp_routes
from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.sqlite.backend import SqliteBackend


def create_app() -> Starlette:
    cfg = load_config(config_path=os.environ["AGENTLENS_IT_CONFIG"], env={})
    url = os.environ["AGENTLENS_IT_DB"]

    async def open_store() -> EngineStore:
        if url.startswith("sqlite:///"):
            lite = SqliteBackend(url.removeprefix("sqlite:///"))
            await lite.open()
            return lite
        pg = PostgresBackend(url, search_path=os.environ.get("AGENTLENS_IT_SCHEMA"), pool_size=4)
        await pg.open()
        await pg.start_change_listener()
        return pg

    engine = build_engine(cfg, open_store)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()

    async def ready(_: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "ready" if engine.ready else "not ready", 200 if engine.ready else 503
        )

    return Starlette(
        routes=[*otlp_routes(engine, cfg), Route("/it/ready", ready)], lifespan=lifespan
    )
