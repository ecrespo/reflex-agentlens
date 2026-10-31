"""Contract-suite harness (plan task P2-04.1; REQ-070.b).

Every contract test receives ``db``, parametrized over the backends: ``sqlite`` always, ``pg``
when ``AGENTLENS_TEST_PG_URL`` is set. Each test gets a fresh, migrated database (SQLite: a copy
of a migrated template file; PostgreSQL: a new schema), which isolates tests more strictly than
one database per module at a small cost. Time is injected through ``db.clock``.

Until the PostgreSQL backend lands (P2-06.1), ``pg`` provides only the migrated engine; tests that
need ``db.backend`` call :meth:`ContractDB.require_backend`, which skips there.
"""

from __future__ import annotations

import secrets
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.store import migrate
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.protocol import StorageBackend
from reflex_agentlens.store.sqlite.backend import SqliteBackend
from tests.contract.dataset import BASE_NS, NS

NOW_NS = BASE_NS + 40 * 86_400 * NS
BACKENDS = [pytest.param("sqlite", id="sqlite"), pytest.param("pg", id="pg", marks=pytest.mark.pg)]


class Clock:
    """Injected clock (Data Model Q7: ``:now_ns`` is injected so tests are deterministic)."""

    def __init__(self, now_ns: int = NOW_NS) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns


@dataclass
class ContractDB:
    name: str
    engine: AsyncEngine  # direct access for fixtures and assertions
    backend: StorageBackend | None
    clock: Clock

    def require_backend(self) -> StorageBackend:
        if self.backend is None:
            pytest.skip(f"no {self.name} StorageBackend yet (PostgreSQL lands in P2-06.1)")
        return self.backend

    async def make_project(self, slug: str | None = None) -> str:
        project_id = new_ulid()
        slug = slug or f"p-{secrets.token_hex(4)}"
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(s.projects).values(
                    id=project_id, slug=slug, name=slug, created_at_ns=0, updated_at_ns=0
                )
            )
        return project_id

    async def scalar(self, sql: str, **params: object) -> object:
        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()


@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated SQLite file, built once per session and copied per test."""
    import asyncio

    path = tmp_path_factory.mktemp("template") / "agentlens.db"

    async def build() -> None:
        backend = SqliteBackend(path)
        await backend.open()
        await backend.migrate()
        await backend.close()  # closing the last connection checkpoints the WAL into the file

    asyncio.run(build())
    return path


@pytest.fixture(params=BACKENDS)
async def db(
    request: pytest.FixtureRequest, sqlite_template: Path, tmp_path: Path
) -> AsyncIterator[ContractDB]:
    clock = Clock()
    if request.param == "sqlite":
        path = tmp_path / "agentlens.db"
        shutil.copyfile(sqlite_template, path)
        backend = SqliteBackend(path, clock=clock)
        await backend.open()
        try:
            yield ContractDB("sqlite", backend.write_engine, backend, clock)
        finally:
            await backend.close()
        return

    pg_url = request.getfixturevalue("pg_url")
    schema = f"c_{secrets.token_hex(6)}"
    admin = create_async_engine(pg_url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(pg_url, connect_args={"server_settings": {"search_path": schema}})
    try:
        await migrate.migrate(engine)
        yield ContractDB("pg", engine, None, clock)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
