"""Migration tests: plan tasks P2-02.1 and P2-02.2 (Data Model §5, REQ-074)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from seed import counts, seed_project
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.core.ids import is_ulid
from reflex_agentlens.store import migrate
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.records import SchemaStatus


def _diff(conn: Connection) -> list[Any]:
    ctx = MigrationContext.configure(
        conn,
        opts={
            "include_object": migrate.include_object_for(conn.dialect.name),
            "compare_type": True,
        },
    )
    return list(compare_metadata(ctx, s.metadata))


def _sqlite_ddl(conn: Connection) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        text("SELECT type, name, sql FROM sqlite_master WHERE tbl_name != 'alembic_version'")
    )
    return {(t, n, sql or "") for t, n, sql in rows}


def _structure(conn: Connection) -> dict[str, Any]:
    """Reflection summary used to compare a migrated database with ``create_all``."""
    insp = inspect(conn)
    out: dict[str, Any] = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
            continue
        cols = insp.get_columns(table)
        idx = insp.get_indexes(table)
        fks = insp.get_foreign_keys(table)
        out[table] = {
            "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in cols],
            "indexes": sorted((i["name"] or "", str(i["column_names"])) for i in idx),
            "checks": sorted(c["name"] or "" for c in insp.get_check_constraints(table)),
            "fks": sorted((f["name"] or "", f["referred_table"]) for f in fks),
        }
    return out


# ── SQLite ───────────────────────────────────────────────────────────────────────────────────


def test_upgrade_head_matches_create_all_sqlite(sqlite_engine: Engine) -> None:
    """P2-02.1 Done: the migrated schema equals ``metadata.create_all`` (by reflection)."""
    with sqlite_engine.begin() as conn:
        migrate.upgrade(conn)
        assert _diff(conn) == []
        migrated = _sqlite_ddl(conn)
    fresh = create_engine("sqlite://")
    with fresh.begin() as conn:
        s.create_all(conn)
        assert _sqlite_ddl(conn) == migrated
    fresh.dispose()


def test_upgrade_writes_app_meta_and_no_pricing(sqlite_engine: Engine) -> None:
    """Data Model §5: instance metadata is inserted; pricing is left to bootstrap (A-11)."""
    with sqlite_engine.begin() as conn:
        migrate.upgrade(conn)
        meta = dict(conn.execute(text("SELECT key, value FROM app_meta")).tuples().all())
        assert set(meta) == {"instance_id", "created_at_ns"}
        assert is_ulid(meta["instance_id"])
        assert int(meta["created_at_ns"]) > 0
        assert conn.execute(text("SELECT count(*) FROM pricing_versions")).scalar_one() == 0


def test_migrated_database_accepts_data_and_cascades(sqlite_engine: Engine) -> None:
    with sqlite_engine.begin() as conn:
        migrate.upgrade(conn)
        project_pk = seed_project(conn)
        conn.execute(text("DELETE FROM projects WHERE pk = :pk"), {"pk": project_pk})
        assert set(counts(conn).values()) == {0}


def test_downgrade_base_removes_everything(sqlite_engine: Engine) -> None:
    with sqlite_engine.begin() as conn:
        migrate.upgrade(conn)
        migrate.downgrade(conn)
        assert inspect(conn).get_table_names() == ["alembic_version"]
        migrate.upgrade(conn)  # and the cycle is repeatable
        assert "spans" in inspect(conn).get_table_names()


def test_head_revision_is_0001() -> None:
    assert migrate.head_revision() == "0001"


# ── PostgreSQL ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.pg
async def test_upgrade_head_matches_create_all_pg(pg_engine: AsyncEngine, pg_url: str) -> None:
    async with pg_engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)
        assert await conn.run_sync(_diff) == []
        migrated = await conn.run_sync(_structure)
        await conn.run_sync(lambda c: seed_project(c))
        await conn.run_sync(migrate.downgrade)
        assert await conn.run_sync(lambda c: inspect(c).get_table_names()) == ["alembic_version"]
        await conn.run_sync(s.create_all)
        assert await conn.run_sync(_structure) == migrated


# ── P2-02.2: status, guarded migration, version checks ───────────────────────────────────────


@pytest.fixture
async def sqlite_async(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agentlens.db'}")
    yield engine
    await engine.dispose()


async def test_empty_database_is_outdated_then_current_after_migrate(
    sqlite_async: AsyncEngine,
) -> None:
    """REQ-074.a/b: an empty or older schema reads OUTDATED; migrate() brings it to CURRENT."""
    assert await migrate.read_schema_status(sqlite_async) is SchemaStatus.OUTDATED
    await migrate.migrate(sqlite_async)
    assert await migrate.read_schema_status(sqlite_async) is SchemaStatus.CURRENT
    await migrate.migrate(sqlite_async)  # idempotent
    assert await migrate.read_schema_status(sqlite_async) is SchemaStatus.CURRENT


async def test_unknown_future_revision_is_newer(sqlite_async: AsyncEngine) -> None:
    """REQ-074.c: a revision the package does not know means the database is newer."""
    await migrate.migrate(sqlite_async)
    async with sqlite_async.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = '9999_future'"))
    assert await migrate.read_schema_status(sqlite_async) is SchemaStatus.NEWER


def test_known_older_revision_is_outdated(
    sqlite_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-074.b: a known revision behind the head reads OUTDATED (head faked one step ahead)."""
    with sqlite_engine.begin() as conn:
        migrate.upgrade(conn)
        monkeypatch.setattr(migrate, "head_revision", lambda: "0002")
        assert migrate.schema_status(conn) is SchemaStatus.OUTDATED


def test_supported_sqlite_passes_the_version_check(sqlite_engine: Engine) -> None:
    with sqlite_engine.connect() as conn:
        migrate.check_backend(conn)


def test_too_old_sqlite_is_refused(sqlite_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate, "_sqlite_version", lambda _c: "3.42.9")
    with sqlite_engine.connect() as conn, pytest.raises(StorageUnavailable, match=r"3\.43"):
        migrate.check_backend(conn)


def test_sqlite_without_fts5_is_refused(
    sqlite_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrate, "_sqlite_has_fts5", lambda _c: False)
    with sqlite_engine.connect() as conn, pytest.raises(StorageUnavailable, match="FTS5"):
        migrate.check_backend(conn)


@pytest.mark.pg
async def test_supported_and_too_old_postgres(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with pg_engine.connect() as conn:
        await conn.run_sync(migrate.check_backend)
        monkeypatch.setattr(migrate, "_pg_version_num", lambda _c: 140012)
        with pytest.raises(StorageUnavailable, match="PostgreSQL 14"):
            await conn.run_sync(migrate.check_backend)


@pytest.mark.pg
async def test_concurrent_migrations_on_postgres(pg_engine: AsyncEngine) -> None:
    """REQ-074.d: one process migrates while the others wait on the lock; all end CURRENT."""
    await asyncio.gather(*(migrate.migrate(pg_engine) for _ in range(3)))
    assert await migrate.read_schema_status(pg_engine) is SchemaStatus.CURRENT
    async with pg_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT count(*) FROM app_meta"))).scalar_one()
    assert rows == 2  # exactly one upgrade ran


@pytest.mark.pg
async def test_migrate_waits_for_the_advisory_lock(pg_engine: AsyncEngine) -> None:
    """REQ-074.d: while another session holds the lock, migrate() does not proceed."""
    key = {"k": migrate.MIGRATE_LOCK_KEY}
    async with pg_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), key)
        task = asyncio.create_task(migrate.migrate(pg_engine))
        await asyncio.sleep(0.5)
        assert not task.done()
        assert await migrate.read_schema_status(pg_engine) is SchemaStatus.OUTDATED
        await holder.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), key)
        await asyncio.wait_for(task, timeout=30)
    assert await migrate.read_schema_status(pg_engine) is SchemaStatus.CURRENT
