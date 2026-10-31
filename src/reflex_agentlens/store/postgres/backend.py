"""PostgreSQL backend on asyncpg (plan task P2-06.1; REQ-072, DD-004, DD-008).

Several processes (``serve`` plus ``collect`` replicas) may write the same database, so the
write path follows the DD-004 concurrency rule (Analyze A-01): Run stubs are created or row-locked
in (``project_pk``, ``trace_id``) order by the ``ON CONFLICT DO UPDATE`` of the write path, Sessions
are locked ``FOR UPDATE`` by ``pk`` after the Runs, and a ``deadlock_detected`` (40P01) retries the
whole batch once before failing it as retryable.

Change feed (P2-10.2, DD-007): every event is also sent with ``pg_notify`` inside its commit
transaction. :meth:`PostgresBackend.start_change_listener` holds one dedicated asyncpg LISTEN
connection that feeds the local broadcaster, dropping events this backend sent itself
(``origin_id``) and events of other schemas sharing the database. While that connection is down
the listener polls a per-Project fingerprint of the Runs every ``poll_interval_s`` and tries to
reconnect after each poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal, TypeVar

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy import Connection, case, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from reflex_agentlens.core.errors import StorageUnavailable
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.timeutil import now_ns
from reflex_agentlens.store import migrate as migrations
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.changefeed import (
    NOTIFY_CHANNEL,
    ChangeFeed,
    decode_notify,
    encode_notify,
)
from reflex_agentlens.store.records import (
    ChangeEvent,
    JobName,
    NormalizedBatch,
    SchemaStatus,
    WriteResult,
)
from reflex_agentlens.store.sqlite.backend import DEFAULT_WINDOW_NS
from reflex_agentlens.store.writeapi import BackendMethods

__all__ = ["DEADLOCK_SQLSTATE", "PostgresBackend", "async_url"]

_R = TypeVar("_R")
_log = logging.getLogger(__name__)
ListenerState = Literal["stopped", "listening", "polling"]
Fingerprint = dict[str, tuple[int, ...]]
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


class PostgresBackend(BackendMethods):
    """``StorageBackend`` on PostgreSQL ≥ 15 (REQ-072)."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 10,
        search_path: str | None = None,
        finalization_window_ns: int = DEFAULT_WINDOW_NS,
        clock: Callable[[], int] = now_ns,
        poll_interval_s: float = 2.0,
    ) -> None:
        self.url = async_url(url)
        self.pool_size = pool_size
        self.search_path = search_path
        self.finalization_window_ns = finalization_window_ns
        self._clock = clock
        self._engine: AsyncEngine | None = None
        self.deadlock_retries = 0  # observable for tests and metrics
        self.changes = ChangeFeed()
        self.origin_id = new_ulid()
        self.poll_interval_s = poll_interval_s
        self.listener_state: ListenerState = "stopped"
        self._listener: asyncio.Task[None] | None = None
        self._listen_conn: asyncpg.Connection | None = None
        self._fingerprint: Fingerprint | None = None

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
        await self._stop_listener()
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

    async def _on_writer(self, fn: Callable[..., _R], *args: Any) -> _R:
        async with self.engine.begin() as conn:
            return await conn.run_sync(fn, *args)

    def job_lock(self, name: JobName) -> AbstractAsyncContextManager[bool]:
        """REQ-092.b: a session advisory lock, so one holder across every process."""
        return self._advisory_job_lock(name)

    @asynccontextmanager
    async def _advisory_job_lock(self, name: str) -> AsyncIterator[bool]:
        key = {"k": f"agentlens:job:{name}"}
        async with self.engine.connect() as conn:
            got = bool(
                (
                    await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), key)
                ).scalar()
            )
            await conn.commit()  # the session lock outlives the transaction
            try:
                yield got
            finally:
                if got:
                    await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), key)
                    await conn.commit()

    async def write_batch(self, batch: NormalizedBatch) -> WriteResult:
        """API §5.1 with the DD-004 lock order; one retry on deadlock, then retryable failure."""
        try:
            return await self._write_and_publish(batch)
        except DBAPIError as exc:
            if _sqlstate(exc) not in (DEADLOCK_SQLSTATE, SERIALIZATION_SQLSTATE):
                raise
            self.deadlock_retries += 1
            try:
                return await self._write_and_publish(batch)
            except DBAPIError as again:
                if _sqlstate(again) in (DEADLOCK_SQLSTATE, SERIALIZATION_SQLSTATE):
                    raise StorageUnavailable("write conflict; retry the batch") from again
                raise

    # ── change feed across processes (P2-10.2) ───────────────────────────────────────────────

    @property
    def _namespace(self) -> str:
        return self.search_path or ""

    def _notify_in_tx(self, conn: Connection, events: Sequence[ChangeEvent]) -> None:
        """``pg_notify`` is transactional: listeners get it when this transaction commits."""
        for event in events:
            conn.execute(
                text("SELECT pg_notify(:c, :p)"),
                {"c": NOTIFY_CHANNEL, "p": encode_notify(event, self.origin_id, self._namespace)},
            )

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: object) -> None:
        notice = decode_notify(str(payload))
        if notice is None or notice.origin_id == self.origin_id:
            return  # malformed, or our own event (already published locally after commit)
        if notice.namespace != self._namespace:
            return  # another deployment in another schema of the same database
        self.changes.publish_nowait(notice.event)

    async def _connect_listener(self) -> asyncpg.Connection:
        dsn = make_url(self.url).set(drivername="postgresql")
        conn: asyncpg.Connection = await asyncpg.connect(dsn.render_as_string(hide_password=False))
        await conn.add_listener(NOTIFY_CHANNEL, self._on_notify)
        return conn

    async def start_change_listener(self) -> None:
        """Start the LISTEN connection (or polling, if it cannot connect); idempotent."""
        if self._listener is not None:
            return
        self._fingerprint = await self._poll_fingerprint()
        await self._try_listen()
        self._listener = asyncio.create_task(self._listen_loop(), name="agentlens-changefeed")

    async def _try_listen(self) -> bool:
        try:
            conn = await self._connect_listener()
        except Exception as exc:  # down: stay on polling, retry after the next poll
            _log.warning("change feed LISTEN connection failed: %s", type(exc).__name__)
            self.listener_state = "polling"
            return False
        self._listen_conn = conn
        self.listener_state = "listening"
        return True

    async def _listen_loop(self) -> None:
        while True:
            conn = self._listen_conn
            if conn is not None and self.listener_state == "listening":
                await self._hold(conn)
                _log.warning("change feed LISTEN connection lost; polling until it is back")
                self._listen_conn = None
                self.listener_state = "polling"
            await asyncio.sleep(self.poll_interval_s)
            await self._poll_once()
            await self._try_listen()

    async def _hold(self, conn: asyncpg.Connection) -> None:
        """Return when the LISTEN connection is gone."""
        # also the baseline the poller compares against once the connection drops
        self._fingerprint = await self._poll_fingerprint() or self._fingerprint
        lost = asyncio.Event()
        conn.add_termination_listener(lambda _c: lost.set())
        while not conn.is_closed():  # the termination callback is not called on every loss
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(lost.wait(), self.poll_interval_s / 4)

    async def _poll_once(self) -> None:
        current = await self._poll_fingerprint()
        if current is None:
            return
        before, self._fingerprint = self._fingerprint or {}, current
        at = self._clock()
        for project in sorted(set(before) | set(current)):
            if before.get(project) != current.get(project):
                self.changes.publish_nowait(
                    ChangeEvent("spans_committed", project, (), truncated=True, committed_at_ns=at)
                )

    async def _poll_fingerprint(self) -> Fingerprint | None:
        """Per Project: Runs, spans, newest span and Runs in progress (cheap, index-backed)."""
        r, p = s.runs, s.projects
        stmt = (
            select(
                p.c.id,
                func.count(r.c.pk),
                func.coalesce(func.sum(r.c.span_count), 0),
                func.coalesce(func.max(r.c.last_span_ns), 0),
                func.coalesce(func.sum(case((r.c.status == "in_progress", 1), else_=0)), 0),
            )
            .select_from(p.outerjoin(r, r.c.project_pk == p.c.pk))
            .group_by(p.c.id)
        )
        try:
            async with self.engine.connect() as conn:
                rows = (await conn.execute(stmt)).all()
        except Exception as exc:  # the database itself is down: try again next interval
            _log.warning("change feed poll failed: %s", type(exc).__name__)
            return None
        return {str(row[0]): tuple(int(v) for v in row[1:]) for row in rows}

    async def _stop_listener(self) -> None:
        task, self._listener = self._listener, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        conn, self._listen_conn = self._listen_conn, None
        if conn is not None and not conn.is_closed():
            with contextlib.suppress(Exception):
                await conn.close(timeout=2)
        self.listener_state = "stopped"
