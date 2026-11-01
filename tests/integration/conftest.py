"""Integration fixtures: a seeded database per backend (SQLite always, PostgreSQL when set)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.contract.conftest import BACKENDS, sqlite_template  # noqa: F401 - re-exported fixture
from tests.integration.live import Database, database


@pytest.fixture(params=BACKENDS)
async def live_db(
    request: pytest.FixtureRequest,
    sqlite_template: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
) -> AsyncIterator[Database]:
    async with database(request.param, sqlite_template, tmp_path) as db:
        await db.seed()
        yield db
