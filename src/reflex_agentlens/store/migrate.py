"""In-process Alembic driver (plan tasks P2-02.1, P2-02.2; Data Model §5; REQ-074).

Migrations always run in-process on a connection the caller owns: the async engine hands a sync
connection to :func:`upgrade` through ``AsyncConnection.run_sync``. Nothing here creates engines.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

from reflex_agentlens.store.schema import SEARCH_TABLE

__all__ = [
    "SCRIPT_LOCATION",
    "alembic_config",
    "downgrade",
    "head_revision",
    "include_object_for",
    "upgrade",
]

SCRIPT_LOCATION = "reflex_agentlens.store:migrations"


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
