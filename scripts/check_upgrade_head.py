"""Migrate an empty database to the package's Alembic head and check it (plan task P2-11.1).

Usage: uv run python scripts/check_upgrade_head.py sqlite
       uv run python scripts/check_upgrade_head.py pg --url postgresql://user:pw@host/db

``sqlite`` uses a fresh file in a temporary directory; ``pg`` uses a fresh schema in the given
database and drops it afterwards. The check passes when ``schema_status`` is ``current`` after
the upgrade, a second upgrade is a no-op, and the backend can write and read a Run.

Exit code: 0 on success, 1 on a failed check.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.store.attrs import attrs_hash
from reflex_agentlens.store.postgres.backend import PostgresBackend, async_url
from reflex_agentlens.store.protocol import StorageBackend
from reflex_agentlens.store.records import (
    JsonObject,
    NewProject,
    NormalizedBatch,
    NormalizedSpan,
    ResourceIn,
    RunQuery,
    SchemaStatus,
    SourceKey,
)
from reflex_agentlens.store.sqlite.backend import SqliteBackend


async def _check(backend: StorageBackend) -> list[str]:
    errors: list[str] = []
    await backend.open()
    try:
        if await backend.schema_status() is not SchemaStatus.OUTDATED:
            errors.append("an empty database should report 'outdated' before the upgrade")
        await backend.migrate()
        await backend.migrate()  # idempotent
        status = await backend.schema_status()
        if status is not SchemaStatus.CURRENT:
            errors.append(f"after upgrade head the schema is {status.value!r}, not 'current'")
            return errors
        project = new_ulid()
        await backend.create_project(NewProject(project, "smoke", "smoke"))
        source = SourceKey("smoke")
        attrs: JsonObject = {"service.name": source.service_name}
        span = NormalizedSpan(
            trace_id=(1).to_bytes(16, "big"),
            span_id=(1).to_bytes(8, "big"),
            parent_span_id=None,
            source=source,
            resource=ResourceIn(attrs_hash(attrs), attrs),
            name="smoke",
            start_ns=1,
            end_ns=2,
            mapping_version=1,
            size_bytes=10,
            received_at_ns=2,
        )
        await backend.write_batch(NormalizedBatch(project, (span,)))
        page = await backend.list_runs(RunQuery(frozenset({project})))
        if len(page.items) != 1:
            errors.append(f"expected 1 Run after a write, got {len(page.items)}")
    finally:
        await backend.close()
    return errors


async def _sqlite() -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        return await _check(SqliteBackend(Path(tmp) / "agentlens.db"))


async def _pg(url: str) -> list[str]:
    schema = f"upgrade_{secrets.token_hex(4)}"
    admin = create_async_engine(async_url(url))
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        return await _check(PostgresBackend(url, search_path=schema, pool_size=2))
    finally:
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("backend", choices=["sqlite", "pg"])
    parser.add_argument("--url", help="PostgreSQL URL (required for pg)")
    args = parser.parse_args(argv)
    if args.backend == "pg" and not args.url:
        parser.error("--url is required for pg")
    errors = asyncio.run(_sqlite() if args.backend == "sqlite" else _pg(args.url))
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if not errors:
        print(f"OK: {args.backend} upgrade head from empty")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
