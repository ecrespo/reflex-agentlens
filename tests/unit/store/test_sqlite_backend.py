"""SQLite backend lifecycle tests: plan task P2-05.1 (REQ-071, DD-003, Data Model §2.0)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.store import migrate as migrations
from reflex_agentlens.store.protocol import StorageBackend
from reflex_agentlens.store.records import SchemaStatus
from reflex_agentlens.store.sqlite.backend import CONNECTION_PRAGMAS, SqliteBackend

EXPECTED = {
    "journal_mode": "wal",
    "synchronous": 1,  # NORMAL
    "foreign_keys": 1,
    "busy_timeout": 5000,
    "temp_store": 2,  # MEMORY
    "cache_size": -65536,
}


def _remove_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[SqliteBackend]:
    b = SqliteBackend(tmp_path / "data" / "agentlens.db", read_pool_size=3)
    await b.open()
    yield b
    await b.close()


def test_backend_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(SqliteBackend(tmp_path / "x.db"), StorageBackend)


async def test_open_creates_the_file_with_wal_and_creation_pragmas(backend: SqliteBackend) -> None:
    """REQ-071.a: the database file is created on open; Data Model §2.0 creation pragmas."""
    assert backend.path.exists()
    async with backend.write_engine.connect() as conn:
        assert (await conn.exec_driver_sql("PRAGMA page_size")).scalar() == 8192
        assert (await conn.exec_driver_sql("PRAGMA auto_vacuum")).scalar() == 2  # INCREMENTAL
    with sqlite3.connect(backend.path) as raw:  # WAL is persistent in the file
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_pragmas_are_set_on_every_pooled_connection(backend: SqliteBackend) -> None:
    """Data Model §2.0: every connection, including each one of the read pool."""
    conns = [await backend.read_engine.connect() for _ in range(backend.read_pool_size)]
    try:
        assert len({id(await c.get_raw_connection()) for c in conns}) == backend.read_pool_size
        for conn in conns:
            for name, _ in CONNECTION_PRAGMAS:
                value = (await conn.exec_driver_sql(f"PRAGMA {name}")).scalar()
                assert value == EXPECTED[name], name
    finally:
        for conn in conns:
            await conn.close()
    async with backend.write_engine.connect() as conn:
        assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1


async def test_write_transactions_take_the_write_lock_up_front(backend: SqliteBackend) -> None:
    """DD-003: BEGIN IMMEDIATE, so another writer is refused while a write transaction is open."""
    await backend.migrate()
    async with backend.write_engine.begin() as conn:
        await conn.exec_driver_sql("SELECT 1")  # no write yet: the lock is already held
        other = sqlite3.connect(backend.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


async def test_ping_reports_health_and_turns_false_when_the_file_disappears(
    backend: SqliteBackend,
) -> None:
    """REQ-330/REQ-340: ping never raises; a vanished database file reads as unhealthy."""
    assert await backend.ping()
    _remove_database_files(backend.path)
    assert not await backend.ping()


async def test_ping_is_false_when_closed(tmp_path: Path) -> None:
    b = SqliteBackend(tmp_path / "a.db")
    assert not await b.ping()
    await b.open()
    await b.close()
    await b.close()  # idempotent
    assert not await b.ping()
    with pytest.raises(StorageUnavailable, match="not open"):
        _ = b.write_engine


async def test_open_is_idempotent_and_never_migrates(backend: SqliteBackend) -> None:
    engine = backend.write_engine
    await backend.open()
    assert backend.write_engine is engine
    assert await backend.schema_status() is SchemaStatus.OUTDATED  # REQ-074.a: no implicit run
    await backend.migrate()
    assert await backend.schema_status() is SchemaStatus.CURRENT


async def test_open_refuses_an_unsupported_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations, "_sqlite_version", lambda _c: "3.40.1")
    b = SqliteBackend(tmp_path / "old.db")
    with pytest.raises(StorageUnavailable, match="too old"):
        await b.open()
    assert not await b.ping()


async def test_open_wraps_driver_errors(tmp_path: Path) -> None:
    (tmp_path / "dir.db").mkdir()
    with pytest.raises(StorageUnavailable, match="cannot open"):
        await SqliteBackend(tmp_path / "dir.db").open()


def test_from_url(tmp_path: Path) -> None:
    assert SqliteBackend.from_url(f"sqlite:///{tmp_path}/a.db").path == tmp_path / "a.db"
    assert SqliteBackend.from_url(f"sqlite+aiosqlite:///{tmp_path}/b.db").path.name == "b.db"
    for bad in ("sqlite://", "sqlite:///:memory:", "postgresql://h/db"):
        with pytest.raises(ValueError, match=r"sqlite|memory"):
            SqliteBackend.from_url(bad)


async def test_concurrent_readers_while_writing(backend: SqliteBackend) -> None:
    """WAL: reads proceed while the write transaction is open."""
    await backend.migrate()
    async with backend.write_engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO purge_log (trigger, started_ns) VALUES ('manual', 1)"
        )

        async def read() -> int:
            async with backend.read_engine.connect() as r:
                return int((await r.exec_driver_sql("SELECT count(*) FROM purge_log")).scalar_one())

        assert list(await asyncio.wait_for(asyncio.gather(read(), read()), timeout=5)) == [0, 0]
