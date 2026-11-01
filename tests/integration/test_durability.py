"""Durability and cross-process behavior (plan task P3-13.2; REQ-009.a, REQ-006.c, REQ-004)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.contract.ingest_rig import now_request
from tests.integration.live import Database, database, live_server
from tests.unit.ingest import otlp


async def test_every_acknowledged_span_survives_sigkill(live_db: Database, tmp_path: Path) -> None:
    """REQ-009.a: a 200 means committed; SIGKILL right after the last ack loses nothing."""
    async with live_server(live_db, tmp_path) as srv:
        for trace in range(1, 21):
            spans = [
                otlp.span(trace=trace, sid=i, parent=None if i == 1 else 1) for i in range(1, 6)
            ]
            resp = await srv.post(now_request(*spans).SerializeToString())
            assert resp.status_code == 200
        srv.kill()
    assert await live_db.count("spans") == 100
    assert await live_db.count("runs") == 20


@pytest.mark.pg
async def test_revocation_reaches_every_process_within_5_s(
    sqlite_template: Path, tmp_path: Path
) -> None:
    """REQ-006.c: two processes on one PostgreSQL database stop accepting a revoked key ≤ 5 s."""
    async with database("pg", sqlite_template, tmp_path) as db:
        await db.seed()
        async with (
            live_server(db, tmp_path, name="a") as a,
            live_server(db, tmp_path, name="b") as b,
        ):
            payload = now_request(otlp.span(trace=1, sid=1)).SerializeToString()
            assert (await a.post(payload)).status_code == 200
            assert (await b.post(payload)).status_code == 200  # duplicate: still 200
            await db.backend.revoke_api_key(db.key_id, time.time_ns())
            revoked = time.monotonic()
            pending = [a, b]
            while pending:
                pending = [srv for srv in pending if (await srv.post(payload)).status_code != 401]
                assert time.monotonic() - revoked <= 5.0, "revocation took more than 5 s"
                await asyncio.sleep(0.1)


async def test_two_collectors_share_one_database(live_db: Database, tmp_path: Path) -> None:
    """REQ-004 (data): exports split across two processes land in the same views."""
    if live_db.name == "sqlite":
        pytest.skip("several collect processes need PostgreSQL (REQ-004.b)")
    async with (
        live_server(live_db, tmp_path, name="a") as a,
        live_server(live_db, tmp_path, name="b") as b,
    ):
        for trace in range(1, 11):
            srv = a if trace % 2 else b
            root = otlp.span(trace=trace, sid=1)
            child = otlp.span(trace=trace, sid=2, parent=1)
            assert (await srv.post(now_request(root).SerializeToString())).status_code == 200
            other = b if srv is a else a
            assert (await other.post(now_request(child).SerializeToString())).status_code == 200
    assert await live_db.count("spans") == 20
    assert await live_db.count("runs") == 10
