"""PostgreSQL backend on asyncpg (plan task P2-06.1; REQ-072, DD-004, DD-008).

Several processes (``serve`` plus ``collect`` replicas) may write the same database, so the
write path follows the DD-004 concurrency rule (Analyze A-01): Run stubs are created or row-locked
in (``project_pk``, ``trace_id``) order by the ``ON CONFLICT DO UPDATE`` of the write path, Sessions
are locked ``FOR UPDATE`` by ``pk`` after the Runs, and a ``deadlock_detected`` (40P01) retries the
whole batch once before failing it as retryable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.core.timeutil import now_ns
from reflex_agentlens.store import migrate as migrations
from reflex_agentlens.store.records import NormalizedBatch, SchemaStatus, WriteResult
from reflex_agentlens.store.sqlite.backend import DEFAULT_WINDOW_NS
from reflex_agentlens.store.writepath import WriteOutcome, write_batch

__all__ = ["DEADLOCK_SQLSTATE", "PostgresBackend", "async_url"]

DEADLOCK_SQLSTATE = "40P01"
SERIALIZATION_SQLSTATE = "40001"


def async_url(url: str) -> str:
    """``postgresql://`` / ``postgres://`` → ``postgresql+asyncpg://``."""
    parsed = make_url(url.replace("postgres://", "postgresql://", 1))
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("expected a postgresql URL")
    return parsed.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


class PostgresBackend:
    """``StorageBackend`` on PostgreSQL ≥ 15 (REQ-072)."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 10,
        search_path: str | None = None,
        finalization_window_ns: int = DEFAULT_WINDOW_NS,
        clock: Callable[[], int] = now_ns,
    ) -> None:
        self.url = async_url(url)
        self.pool_size = pool_size
        self.search_path = search_path
        self.finalization_window_ns = finalization_window_ns
        self._clock = clock
        self._engine: AsyncEngine | None = None
        self.deadlock_retries = 0  # observable for tests and metrics

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise StorageUnavailable("backend is not open")
        return self._engine

    # The SQLite backend exposes two engines; on PostgreSQL both names are the same pool.
    write_engine = engine
    read_engine = engine

    async def open(self) -> None:
        if self._engine is not None:
            return
        kwargs: dict[str, Any] = {"pool_size": self.pool_size, "pool_pre_ping": True}
        if self.search_path:
            kwargs["connect_args"] = {"server_settings": {"search_path": self.search_path}}
        engine = create_async_engine(self.url, **kwargs)
        try:
            async with engine.connect() as conn:
                await conn.run_sync(migrations.check_backend)
        except StorageUnavailable:
            await engine.dispose()
            raise
        except Exception as exc:
            await engine.dispose()
            raise StorageUnavailable("cannot connect to PostgreSQL") from exc
        self._engine = engine

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None

    async def ping(self) -> bool:
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
        except Exception:  # ping reports health; it never raises
            return False
        return True

    async def schema_status(self) -> SchemaStatus:
        return await migrations.read_schema_status(self.engine)

    async def migrate(self) -> None:
        await migrations.migrate(self.engine)

    async def _write_once(self, batch: NormalizedBatch) -> WriteOutcome:
        async with self.engine.begin() as conn:
            return await conn.run_sync(
                write_batch, batch, now_ns=self._clock(), window_ns=self.finalization_window_ns
            )

    async def write_batch(self, batch: NormalizedBatch) -> WriteResult:
        """API §5.1 with the DD-004 lock order; one retry on deadlock, then retryable failure."""
        try:
            outcome = await self._write_once(batch)
        except DBAPIError as exc:
            if _sqlstate(exc) not in (DEADLOCK_SQLSTATE, SERIALIZATION_SQLSTATE):
                raise
            self.deadlock_retries += 1
            try:
                outcome = await self._write_once(batch)
            except DBAPIError as again:
                if _sqlstate(again) in (DEADLOCK_SQLSTATE, SERIALIZATION_SQLSTATE):
                    raise StorageUnavailable("write conflict; retry the batch") from again
                raise
        return WriteResult(
            inserted=outcome.inserted,
            duplicates=outcome.duplicates,
            affected_runs=outcome.affected_runs,
            committed_at_ns=self._clock(),
        )
