"""SQLite backend: engines, lifecycle and the write path (plan tasks P2-05.1, P2-05.2).

- One **write** connection (``pool_size=1``) whose transactions start with ``BEGIN IMMEDIATE``,
  so the write lock is taken up front and there are no lock upgrades mid-transaction (DD-003).
  An ``asyncio.Lock`` serializes writers inside the process.
- A **read** pool of ``read_pool_size`` connections; WAL lets them read while the writer writes.
- Data Model §2.0 pragmas on **every** connection; ``page_size`` and ``auto_vacuum`` only when
  the database file is still empty; ``journal_mode=WAL`` (persistent, idempotent).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.core.timeutil import NS_PER_S, now_ns
from reflex_agentlens.store import migrate as migrations
from reflex_agentlens.store.reads import ReadMethods
from reflex_agentlens.store.records import NormalizedBatch, SchemaStatus, WriteResult
from reflex_agentlens.store.writepath import write_batch

__all__ = ["CONNECTION_PRAGMAS", "CREATION_PRAGMAS", "SqliteBackend"]

CONNECTION_PRAGMAS = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", "5000"),
    ("temp_store", "MEMORY"),
    ("cache_size", "-65536"),
)
CREATION_PRAGMAS = (("page_size", "8192"), ("auto_vacuum", "INCREMENTAL"))
DEFAULT_WINDOW_NS = int(timedelta(minutes=5).total_seconds()) * NS_PER_S


def _on_connect(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA page_count")
        if cur.fetchone()[0] == 0:  # brand-new file: these only take effect before any table
            for name, value in CREATION_PRAGMAS:
                cur.execute(f"PRAGMA {name}={value}")
        for name, value in CONNECTION_PRAGMAS:
            cur.execute(f"PRAGMA {name}={value}")
            if name == "journal_mode":
                cur.fetchall()
    finally:
        cur.close()


def _write_mode(dbapi_conn: Any, _record: Any) -> None:
    dbapi_conn.isolation_level = None  # the driver must not BEGIN on its own


def _begin_immediate(conn: Any) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE")


class SqliteBackend(ReadMethods):
    """``StorageBackend`` on one SQLite file (REQ-071)."""

    def __init__(
        self,
        path: str | Path,
        *,
        read_pool_size: int = 4,
        finalization_window_ns: int = DEFAULT_WINDOW_NS,
        clock: Callable[[], int] = now_ns,
    ) -> None:
        self.path = Path(path)
        self.read_pool_size = read_pool_size
        self.finalization_window_ns = finalization_window_ns
        self._clock = clock
        self._write: AsyncEngine | None = None
        self._read: AsyncEngine | None = None
        self._write_lock = asyncio.Lock()

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> SqliteBackend:
        """Build from ``sqlite:///path`` or ``sqlite+aiosqlite:///path`` (no in-memory DBs)."""
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite" or not parsed.database:
            raise ValueError("expected a sqlite URL with a file path")
        if parsed.database == ":memory:":
            raise ValueError("in-memory SQLite is not supported; give a file path")
        return cls(parsed.database, **kwargs)

    # ── engines ──────────────────────────────────────────────────────────────────────────────

    @property
    def write_engine(self) -> AsyncEngine:
        if self._write is None:
            raise StorageUnavailable("backend is not open")
        return self._write

    @property
    def read_engine(self) -> AsyncEngine:
        if self._read is None:
            raise StorageUnavailable("backend is not open")
        return self._read

    def _engine(self, pool_size: int, *, writer: bool) -> AsyncEngine:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path}", pool_size=pool_size, max_overflow=0
        )
        event.listen(engine.sync_engine, "connect", _on_connect)
        if writer:
            event.listen(engine.sync_engine, "connect", _write_mode)
            event.listen(engine.sync_engine, "begin", _begin_immediate)
        return engine

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

    async def open(self) -> None:
        if self._write is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write = self._engine(1, writer=True)
        try:
            async with write.connect() as conn:
                await conn.run_sync(migrations.check_backend)
                await conn.rollback()
        except StorageUnavailable:
            await write.dispose()
            raise
        except Exception as exc:
            await write.dispose()
            raise StorageUnavailable("cannot open the SQLite database") from exc
        self._write = write
        self._read = self._engine(self.read_pool_size, writer=False)

    async def close(self) -> None:
        for engine in (self._read, self._write):
            if engine is not None:
                await engine.dispose()
        self._read = self._write = None

    async def ping(self) -> bool:
        if self._read is None or not self.path.exists():
            return False
        try:
            async with self._read.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
        except Exception:
            return False
        return True

    async def schema_status(self) -> SchemaStatus:
        return await migrations.read_schema_status(self.read_engine)

    async def migrate(self) -> None:
        async with self._write_lock:
            await migrations.migrate(self.write_engine)

    # ── ingest path ──────────────────────────────────────────────────────────────────────────

    async def write_batch(self, batch: NormalizedBatch) -> WriteResult:
        """API §5.1 in one ``BEGIN IMMEDIATE`` transaction on the single write connection."""
        async with self._write_lock, self.write_engine.begin() as conn:
            outcome = await conn.run_sync(
                write_batch,
                batch,
                now_ns=self._clock(),
                window_ns=self.finalization_window_ns,
            )
        return WriteResult(
            inserted=outcome.inserted,
            duplicates=outcome.duplicates,
            affected_runs=outcome.affected_runs,
            committed_at_ns=self._clock(),
        )
