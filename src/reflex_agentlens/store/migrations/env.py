"""Alembic environment (plan task P2-02.1, Data Model §5).

Normal use is in-process: ``store.migrate`` passes an open *sync* connection (from an async
engine via ``run_sync``) as ``config.attributes["connection"]``. For development tooling
(``alembic revision --autogenerate``) a ``sqlalchemy.url`` option is honoured instead.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from reflex_agentlens.store.migrate import include_object_for
from reflex_agentlens.store.schema import metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        include_object=include_object_for(connection.dialect.name),
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        compare_server_default=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_with_url(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run)
            await conn.commit()
    finally:
        await engine.dispose()


def main() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("no connection or sqlalchemy.url given to the Alembic environment")
    asyncio.run(_run_with_url(url))


if context.is_offline_mode():  # pragma: no cover - offline SQL scripts are not supported
    raise RuntimeError("offline migrations are not supported")
main()
