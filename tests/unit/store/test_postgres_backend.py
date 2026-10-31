"""PostgreSQL backend unit tests: plan task P2-06.1 (REQ-072, DD-004 retry policy)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.store.postgres.backend import PostgresBackend, async_url
from reflex_agentlens.store.protocol import StorageBackend
from reflex_agentlens.store.records import NormalizedBatch, SchemaStatus, WriteResult
from reflex_agentlens.store.writepath import WriteOutcome


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("stmt", {}, _Orig(sqlstate))


def test_async_url_normalizes_schemes() -> None:
    assert async_url("postgresql://u:p@h:5/db") == "postgresql+asyncpg://u:p@h:5/db"
    assert async_url("postgres://h/db") == "postgresql+asyncpg://h/db"
    assert async_url("postgresql+asyncpg://h/db") == "postgresql+asyncpg://h/db"
    with pytest.raises(ValueError, match="postgresql"):
        async_url("sqlite:///x.db")


def test_backend_satisfies_the_protocol() -> None:
    assert isinstance(PostgresBackend("postgresql://h/db"), StorageBackend)


async def test_closed_backend() -> None:
    b = PostgresBackend("postgresql://h/db")
    assert not await b.ping()
    await b.close()
    with pytest.raises(StorageUnavailable, match="not open"):
        _ = b.engine


async def test_unreachable_server_is_storage_unavailable() -> None:
    b = PostgresBackend("postgresql://u:p@127.0.0.1:1/none")
    with pytest.raises(StorageUnavailable, match="cannot connect"):
        await b.open()


def _scripted(b: PostgresBackend, script: list[Any]) -> list[int]:
    calls: list[int] = []

    async def fake(batch: NormalizedBatch) -> WriteResult:
        calls.append(1)
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return WriteResult(step.inserted, step.duplicates, step.affected_runs, b._clock())

    b._write_and_publish = fake  # type: ignore[method-assign]
    return calls


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
async def test_deadlock_retries_the_batch_once(sqlstate: str) -> None:
    """DD-004: one group retry on deadlock_detected / serialization failure."""
    b = PostgresBackend("postgresql://h/db", clock=lambda: 5)
    calls = _scripted(b, [_db_error(sqlstate), WriteOutcome(3, 1, [])])
    res = await b.write_batch(NormalizedBatch("p", ()))
    assert (res.inserted, res.duplicates, res.committed_at_ns) == (3, 1, 5)
    assert (len(calls), b.deadlock_retries) == (2, 1)


async def test_second_deadlock_fails_the_batch_as_retryable() -> None:
    b = PostgresBackend("postgresql://h/db")
    _scripted(b, [_db_error("40P01"), _db_error("40P01")])
    with pytest.raises(StorageUnavailable, match="retry"):
        await b.write_batch(NormalizedBatch("p", ()))


@pytest.mark.parametrize("script", [[_db_error("23505")], [_db_error("40P01"), _db_error("23505")]])
async def test_other_errors_propagate(script: list[Any]) -> None:
    b = PostgresBackend("postgresql://h/db")
    _scripted(b, script)
    with pytest.raises(DBAPIError):
        await b.write_batch(NormalizedBatch("p", ()))


@pytest.mark.pg
async def test_lifecycle_against_a_server(pg_url: str) -> None:
    b = PostgresBackend(pg_url, search_path="lifecycle_unused")
    await b.open()
    await b.open()  # idempotent
    try:
        assert await b.ping()
        assert b.read_engine is b.write_engine is b.engine
        assert await b.schema_status() is SchemaStatus.OUTDATED  # empty search_path schema
    finally:
        await b.close()
    assert not await b.ping()
