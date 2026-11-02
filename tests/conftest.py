"""Shared pytest configuration (plan task P1-02.3, REQ-361.a).

- Hypothesis profiles: ``ci`` (more examples, derandomized, no deadline) and ``dev`` (fast).
  Selected with ``HYPOTHESIS_PROFILE``; defaults to ``dev``.
- Tests marked ``pg`` are skipped unless ``AGENTLENS_TEST_PG_URL`` is set; tests marked ``smoke``
  (browser, built frontend) unless ``AGENTLENS_SMOKE=1`` (the CI ``smoke`` job); tests marked
  ``packaging`` unless ``AGENTLENS_WHEEL`` names a built wheel (the CI ``wheel`` job); tests
  marked ``docker`` unless ``AGENTLENS_IMAGE`` names a built image (the CI ``docker`` job).
- Database fixtures: ``sqlite_engine`` (in-memory, foreign keys on) and ``pg_engine`` (a fresh
  schema per test, dropped afterwards).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Iterator

import pytest
from hypothesis import settings
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

settings.register_profile("ci", max_examples=300, derandomize=True, deadline=None)
settings.register_profile("dev", max_examples=50, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

PG_URL_ENV = "AGENTLENS_TEST_PG_URL"


SMOKE_ENV = "AGENTLENS_SMOKE"
WHEEL_ENV = "AGENTLENS_WHEEL"
IMAGE_ENV = "AGENTLENS_IMAGE"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_pg = pytest.mark.skip(reason=f"{PG_URL_ENV} is not set")
    skip_smoke = pytest.mark.skip(reason=f"browser smoke test: set {SMOKE_ENV}=1")
    skip_wheel = pytest.mark.skip(reason=f"packaging test: set {WHEEL_ENV} to a built wheel")
    skip_image = pytest.mark.skip(reason=f"docker test: set {IMAGE_ENV} to a built image")
    for item in items:
        if "pg" in item.keywords and not os.environ.get(PG_URL_ENV):
            item.add_marker(skip_pg)
        if "smoke" in item.keywords and os.environ.get(SMOKE_ENV) != "1":
            item.add_marker(skip_smoke)
        if "packaging" in item.keywords and not os.environ.get(WHEEL_ENV):
            item.add_marker(skip_wheel)
        if "docker" in item.keywords and not os.environ.get(IMAGE_ENV):
            item.add_marker(skip_image)


@pytest.fixture
def pg_url() -> str:
    """PostgreSQL URL for ``pg`` tests (only reached when the variable is set)."""
    return os.environ[PG_URL_ENV]


def _enable_fks(dbapi_conn: object, _record: object) -> None:
    cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def sqlite_engine_for(url: str) -> Engine:
    engine = create_engine(url)
    event.listen(engine, "connect", _enable_fks)
    return engine


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """In-memory SQLite (one shared connection) with ``PRAGMA foreign_keys=ON``."""
    engine = sqlite_engine_for("sqlite://")
    yield engine
    engine.dispose()


@pytest.fixture
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    """An async engine bound to a fresh, uniquely named schema, dropped afterwards."""
    schema = f"t_{secrets.token_hex(6)}"
    admin = create_async_engine(pg_url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(pg_url, connect_args={"server_settings": {"search_path": schema}})
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
