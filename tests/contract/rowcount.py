"""Rows-fetched recorder for the bounded-aggregates rule (plan task P2-04.1, REQ-106.h).

While active, every plain ``SELECT`` an engine executes is counted by running
``SELECT count(*) FROM (<statement>)`` with the same parameters on the same connection, so the
helper knows how many rows the backend pulled into Python without touching the backend code.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class RowsFetched:
    statements: list[tuple[str, int]] = field(default_factory=list)

    @property
    def max_rows(self) -> int:
        return max((n for _, n in self.statements), default=0)

    def assert_bounded(self, limit: int) -> None:
        """Fail when any statement returned more than ``limit`` rows (REQ-106.h)."""
        over = [(sql, n) for sql, n in self.statements if n > limit]
        assert not over, f"unbounded fetch: {over[0][1]} rows > {limit} for {over[0][0][:200]}"


@contextmanager
def rows_fetched(engine: AsyncEngine) -> Iterator[RowsFetched]:
    record = RowsFetched()
    counting = False

    def after(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        nonlocal counting
        if counting or executemany or not statement.lstrip().upper().startswith("SELECT"):
            return
        counting = True
        try:
            cur = conn.connection.dbapi_connection.cursor()
            try:
                cur.execute(f"SELECT count(*) FROM ({statement}) AS counted", parameters)  # noqa: S608
                record.statements.append((statement, int(cur.fetchone()[0])))
            finally:
                cur.close()
        finally:
            counting = False

    event.listen(engine.sync_engine, "after_cursor_execute", after)
    try:
        yield record
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", after)
