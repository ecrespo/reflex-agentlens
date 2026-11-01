"""Fresh (empty, unmigrated) databases for the runtime tests: a SQLite file or a PG schema."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from reflex_agentlens.core.config import AgentlensConfig, load_config
from tests.contract.conftest import BACKENDS


@dataclass
class Fresh:
    name: str
    tmp_path: Path
    url: str
    options: dict[str, Any] = field(default_factory=dict)  # backend constructor options

    def config(self, **overrides: object) -> AgentlensConfig:
        base: dict[str, object] = {
            "server.data_dir": str(self.tmp_path / "data"),
            "database.url": self.url,
            "server.grpc_enabled": False,
        }
        base.update(overrides)
        return load_config(env={}, cwd=self.tmp_path, cli_overrides=base)

    @property
    def runtime_options(self) -> dict[str, Any]:
        return {"backend_options": self.options}


@pytest.fixture(params=BACKENDS)
async def fresh(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Fresh]:
    if request.param == "sqlite":
        yield Fresh("sqlite", tmp_path, f"sqlite:///{tmp_path / 'agentlens.db'}")
        return
    pg_url = os.environ["AGENTLENS_TEST_PG_URL"]
    schema = f"rt_{secrets.token_hex(6)}"
    admin = create_async_engine(pg_url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        yield Fresh("pg", tmp_path, pg_url, {"search_path": schema, "pool_size": 4})
    finally:
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
