"""In-process Alembic driver (plan tasks P2-02.1, P2-02.2; Data Model §5; REQ-074).

Migrations always run in-process on a connection the caller owns: the async engine hands a sync
connection to :func:`upgrade` through ``AsyncConnection.run_sync``. Nothing here creates engines.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Connection, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.store.records import SchemaStatus
from reflex_agentlens.store.schema import SEARCH_TABLE

__all__ = [
    "MIGRATE_LOCK_KEY",
    "MIN_PG_VERSION_NUM",
    "MIN_SQLITE_VERSION",
    "SCRIPT_LOCATION",
    "alembic_config",
    "check_backend",
    "current_revision",
    "downgrade",
    "head_revision",
    "include_object_for",
    "migrate",
    "read_schema_status",
    "schema_status",
    "upgrade",
]

SCRIPT_LOCATION = "reflex_agentlens.store:migrations"
MIGRATE_LOCK_KEY = "agentlens:migrate"
MIN_SQLITE_VERSION = (3, 43, 0)
MIN_PG_VERSION_NUM = 150000


def include_object_for(dialect: str) -> Callable[[Any, str | None, str, bool, Any], bool]:
    """Autogenerate filter for ``dialect``.

    Skips ``span_search`` and the FTS5 shadow tables (created by hand per backend) and schema
    items declared with ``ddl_if`` for another dialect, which Alembic does not evaluate itself.
    """

    def include_object(
        obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        if type_ == "table" and name is not None and name.startswith(SEARCH_TABLE):
            return False
        table = getattr(obj, "table", None)
        if table is not None and str(table.name).startswith(SEARCH_TABLE):
            return False
        ddl_if = getattr(obj, "_ddl_if", None)
        return ddl_if is None or ddl_if.dialect in (None, dialect)

    return include_object


def alembic_config(connection: Connection | None = None) -> Config:
    """An Alembic config pointing at the packaged environment, bound to ``connection``."""
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    """The package's single migration head."""
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - the package always ships 0001
        raise RuntimeError("no migration head found")
    return head


def upgrade(connection: Connection, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` on ``connection`` (the caller commits)."""
    command.upgrade(alembic_config(connection), revision)


def downgrade(connection: Connection, revision: str = "base") -> None:
    """Development only: releases never downgrade (Data Model §5 policy)."""
    command.downgrade(alembic_config(connection), revision)


# ── status (REQ-074.a-c) ─────────────────────────────────────────────────────────────────────


def current_revision(connection: Connection) -> str | None:
    """The revision stamped in ``alembic_version``; ``None`` for an empty database."""
    return MigrationContext.configure(connection).get_current_revision()


def schema_status(connection: Connection) -> SchemaStatus:
    """Compare the database revision with the package head.

    ``OUTDATED`` covers an empty database and any known older revision; a revision this package
    does not know can only come from a newer release, hence ``NEWER`` (REQ-074.c).
    """
    current = current_revision(connection)
    if current == head_revision():
        return SchemaStatus.CURRENT
    if current is None:
        return SchemaStatus.OUTDATED
    try:
        known = ScriptDirectory.from_config(alembic_config()).get_revision(current)
    except CommandError:
        known = None
    return SchemaStatus.OUTDATED if known is not None else SchemaStatus.NEWER


async def read_schema_status(engine: AsyncEngine) -> SchemaStatus:
    async with engine.connect() as conn:
        return await conn.run_sync(schema_status)


# ── guarded migration (REQ-074.a, REQ-074.d) ─────────────────────────────────────────────────


async def migrate(engine: AsyncEngine) -> None:
    """Upgrade to head. On PostgreSQL, under a session advisory lock (Data Model §5).

    Concurrent callers queue on the lock; each then finds the schema at head and the upgrade is
    a no-op, so every caller returns with a ``CURRENT`` schema. On SQLite only one process owns
    the database, so the upgrade runs directly.
    """
    async with engine.connect() as conn:
        locked = conn.dialect.name == "postgresql"
        if locked:
            await conn.execute(
                text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": MIGRATE_LOCK_KEY}
            )
            await conn.commit()
        try:
            await conn.run_sync(upgrade)
            await conn.commit()
        finally:
            if locked:
                await conn.rollback()
                await conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": MIGRATE_LOCK_KEY}
                )
                await conn.commit()


# ── backend version checks (TD §9.2) ─────────────────────────────────────────────────────────


def _sqlite_version(connection: Connection) -> str:
    return str(connection.exec_driver_sql("SELECT sqlite_version()").scalar_one())


def _sqlite_has_fts5(connection: Connection) -> bool:
    try:
        connection.exec_driver_sql("CREATE VIRTUAL TABLE temp.agentlens_fts5_probe USING fts5(x)")
    except OperationalError:
        return False
    connection.exec_driver_sql("DROP TABLE temp.agentlens_fts5_probe")
    return True


def _pg_version_num(connection: Connection) -> int:
    return int(connection.exec_driver_sql("SHOW server_version_num").scalar_one())


def _parse_version(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3] if part.isdigit())


def check_backend(connection: Connection) -> None:
    """Refuse unsupported databases: SQLite ≥ 3.43 with FTS5, PostgreSQL ≥ 15.

    Raises:
        StorageUnavailable: with the reason, for an old version or a missing feature.
    """
    if connection.dialect.name == "sqlite":
        version = _sqlite_version(connection)
        if _parse_version(version) < MIN_SQLITE_VERSION:
            raise StorageUnavailable(f"SQLite {version} is too old; 3.43 or newer is required")
        if not _sqlite_has_fts5(connection):
            raise StorageUnavailable("SQLite was built without FTS5, which search requires")
        return
    if connection.dialect.name == "postgresql":
        num = _pg_version_num(connection)
        if num < MIN_PG_VERSION_NUM:
            major = num // 10000
            raise StorageUnavailable(f"PostgreSQL {major} is too old; 15 or newer is required")
        return
    raise StorageUnavailable(f"unsupported database dialect {connection.dialect.name!r}")
