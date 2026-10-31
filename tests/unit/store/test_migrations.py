"""Migration tests: plan tasks P2-02.1 and P2-02.2 (Data Model §5, REQ-074)."""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from seed import counts, seed_project
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from reflex_agentlens.core.ids import is_ulid
from reflex_agentlens.store import migrate
from reflex_agentlens.store import schema as s


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
