"""Store test fixtures: SQLite engines with foreign keys on, and isolated PostgreSQL schemas."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


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
