"""Fresh (empty, unmigrated) databases for the runtime tests: a SQLite file or a PG schema."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from reflex_agentlens.core.config import AgentlensConfig, load_config
from tests.contract.conftest import BACKENDS


@dataclass
class Fresh:
    name: str
    tmp_path: Path
    url: str
    options: dict[str, Any] = field(default_factory=dict)  # backend constructor options

    def env(self, **extra: str) -> dict[str, str]:
        """The same database through environment variables (CLI and subprocess tests)."""
        return {
            "HOME": str(self.tmp_path),
            "AGENTLENS_DATABASE_URL": self.url,
            "AGENTLENS_SERVER_DATA_DIR": str(self.tmp_path / "data"),
            "AGENTLENS_DATABASE_POOL_SIZE": "4",
            **extra,
        }

    def config(self, **overrides: object) -> AgentlensConfig:
        base: dict[str, object] = {
            "server.data_dir": str(self.tmp_path / "data"),
            "database.url": self.url,
            "server.grpc_enabled": False,
            "database.pool_size": 4,
        }
        base.update(overrides)
        return load_config(env={}, cwd=self.tmp_path, cli_overrides=base)

    @property
    def runtime_options(self) -> dict[str, Any]:
        return {"backend_options": self.options}


@pytest.fixture(params=BACKENDS)
async def fresh(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Fresh]:
    """SQLite: a new file. PostgreSQL: a new database, so the URL alone isolates the test
    (subprocesses and the CLI cannot take a search path)."""
    if request.param == "sqlite":
        yield Fresh("sqlite", tmp_path, f"sqlite:///{tmp_path / 'agentlens.db'}")
        return
    pg_url = make_url(os.environ["AGENTLENS_TEST_PG_URL"])
    name = f"rt_{secrets.token_hex(6)}"
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"CREATE DATABASE {name}"))
    url = pg_url.set(database=name).render_as_string(hide_password=False)
    try:
        yield Fresh("pg", tmp_path, url)
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        await admin.dispose()
