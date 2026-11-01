"""A running ingest engine over a real SQLite database, for endpoint tests (P3-09.1, P3-10.1)."""

from __future__ import annotations

import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from sqlalchemy import text

from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.security import generate_api_key, hash_secret
from reflex_agentlens.ingest.core import IngestCore, build_engine
from reflex_agentlens.ingest.engine import EngineRuntime, IngestEngine, Service
from reflex_agentlens.store.records import NewApiKeyRecord, NewProject, ProjectPatch
from reflex_agentlens.store.sqlite.backend import SqliteBackend
from tests.unit.ingest import otlp


@dataclass
class IngestRig:
    engine: IngestEngine
    cfg: AgentlensConfig
    project_id: str
    key: str
    key_id: str

    @property
    def rt(self) -> EngineRuntime:
        rt = self.engine.runtime
        assert rt is not None
        return rt

    @property
    def core(self) -> IngestCore:
        return cast(IngestCore, self.rt.app)

    async def scalar(self, sql: str) -> Any:
        async def run(rt: EngineRuntime) -> Any:
            backend = cast(SqliteBackend, rt.store)
            async with backend.write_engine.connect() as conn:
                return (await conn.execute(text(sql))).scalar()

        return await self.engine.call(run)

    async def store_call(self, method: str, *args: Any) -> Any:
        return await self.engine.call(lambda rt: getattr(rt.store, method)(*args))


def now_request(*spans: Any, **kw: Any) -> ExportTraceServiceRequest:
    """``otlp.request`` with span times moved from the fixed ``NOW_NS`` to the real clock."""
    shift = time.time_ns() - otlp.NOW_NS
    for sp in spans:
        sp.start_time_unix_nano += shift
        sp.end_time_unix_nano += shift
    return otlp.request(*spans, **kw)


@asynccontextmanager
async def ingest_rig(
    sqlite_template: Path,
    tmp_path: Path,
    toml: str = "",
    *,
    patch: ProjectPatch | None = None,
    extra_services: list[Service] | None = None,
) -> AsyncIterator[IngestRig]:
    path = tmp_path / "agentlens.db"
    shutil.copyfile(sqlite_template, path)
    config = tmp_path / "agentlens.toml"
    config.write_text('[server]\ngrpc_enabled = false\nshutdown_grace = "2s"\n' + toml)
    cfg = load_config(config_path=config, env={})

    async def open_store() -> SqliteBackend:
        backend = SqliteBackend(path)
        await backend.open()
        return backend

    engine = build_engine(cfg, open_store, extra_services=extra_services)
    await engine.start()
    try:
        key = generate_api_key()

        async def seed(rt: EngineRuntime) -> str:
            store = cast(SqliteBackend, rt.store)
            project = await store.create_project(NewProject(new_ulid(), "demo", "Demo"))
            if patch is not None:
                await store.update_project(project.id, patch)
            h = hash_secret(key.secret)
            await store.create_api_key(
                NewApiKeyRecord(key.key_id, project.id, h.salt, h.digest, time.time_ns())
            )
            return project.id

        project_id = await engine.call(seed)
        yield IngestRig(engine, cfg, project_id, key.plaintext, key.key_id)
    finally:
        await engine.stop()
