"""Ingest on real endpoints: a Granian process, HTTP + gRPC ports, a real database (P3-13.1).

REQ-001…REQ-010, REQ-009.a/b, REQ-330, Analyze A-04. Runs on SQLite and, when
``AGENTLENS_TEST_PG_URL`` is set, on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import gzip
import time
import zlib
from pathlib import Path

import grpc  # type: ignore[import-untyped]
import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

from reflex_agentlens.store.records import ProjectPatch
from tests.contract.ingest_rig import now_request
from tests.integration.live import Database, TcpProxy, database, live_server
from tests.unit.ingest import otlp


def body(n: int = 3, trace: int = 1) -> bytes:
    spans = [otlp.span(trace=trace, sid=i, parent=None if i == 1 else 1) for i in range(1, n + 1)]
    return now_request(*spans).SerializeToString()


async def test_http_json_gzip_and_grpc_all_commit(live_db: Database, tmp_path: Path) -> None:
    """REQ-001.a/b, REQ-002, REQ-003: every acknowledged span is stored."""
    async with live_server(live_db, tmp_path) as srv:
        assert (await srv.post(body(3, trace=1))).status_code == 200
        assert (
            await srv.post(gzip.compress(body(2, trace=2)), content_encoding="gzip")
        ).status_code == 200
        js = otlp.to_otlp_json(now_request(otlp.span(trace=3, sid=1)))
        resp = await srv.post(js, content_type="application/json")
        assert (resp.status_code, resp.json()) == (200, {})
        async with grpc.aio.insecure_channel(f"127.0.0.1:{srv.grpc_port}") as channel:
            stub = trace_service_pb2_grpc.TraceServiceStub(channel)
            await stub.Export(
                now_request(otlp.span(trace=4, sid=1)),
                metadata=[("authorization", f"Bearer {live_db.key}")],
                compression=grpc.Compression.Gzip,
                timeout=10,
            )
    assert await live_db.count("spans") == 7


async def test_authentication(live_db: Database, tmp_path: Path) -> None:
    """REQ-006.b on both protocols."""
    async with live_server(live_db, tmp_path) as srv:
        assert (await srv.post(body(), key="al_nope")).status_code == 401
        async with grpc.aio.insecure_channel(f"127.0.0.1:{srv.grpc_port}") as channel:
            stub = trace_service_pb2_grpc.TraceServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await stub.Export(now_request(otlp.span()), timeout=10)
        assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert await live_db.count("spans") == 0


async def test_rate_limit(live_db: Database, tmp_path: Path) -> None:
    """REQ-007.a: 429 with Retry-After, nothing persisted from the refused request."""
    await live_db.backend.update_project(
        live_db.project_id, ProjectPatch(request_rate=1, request_burst=2)
    )
    async with live_server(live_db, tmp_path) as srv:
        codes = [(await srv.post(body(1, trace=t))).status_code for t in (1, 2, 3)]
        assert codes == [200, 200, 429]
    assert await live_db.count("spans") == 2


async def test_zip_bomb_is_refused_and_the_server_stays_up(
    live_db: Database, tmp_path: Path
) -> None:
    """REQ-007.c."""
    deflater = zlib.compressobj(9, zlib.DEFLATED, 31)
    bomb = b"".join(deflater.compress(bytes(1 << 20)) for _ in range(256)) + deflater.flush()
    async with live_server(live_db, tmp_path) as srv:
        assert (await srv.post(bomb, content_encoding="gzip")).status_code == 413
        assert (await srv.post(body())).status_code == 200


async def test_backpressure_is_503(live_db: Database, tmp_path: Path) -> None:
    """REQ-009.b/c: a batch that does not fit the queue budget is refused, not queued."""
    async with live_server(live_db, tmp_path, "[ingest]\nqueue_memory_bytes = 2000\n") as srv:
        resp = await srv.post(body(10))
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "2"
        assert (await srv.post(body(1, trace=9))).status_code == 200
    assert await live_db.count("spans") == 1


async def test_project_deleted_mid_ingest(live_db: Database, tmp_path: Path) -> None:
    """A-04: a request for a Project deleted after its key was cached persists nothing.

    SQLite has no cross-process notification, so the server still holds the key and settings
    (≤ 5 s) and the batch is rolled back to its savepoint: ``BatchRejected`` → 400. PostgreSQL
    notifies ``settings_changed``, so the key is re-checked and refused: 401.
    """
    async with live_server(live_db, tmp_path) as srv:
        assert (await srv.post(body(2, trace=1))).status_code == 200
        await live_db.backend.delete_project(live_db.project_id)
        await asyncio.sleep(0.3)
        resp = await srv.post(body(2, trace=2))
        expected = 400 if live_db.name == "sqlite" else 401
        assert resp.status_code == expected
        assert (await srv.post(body(1, trace=3))).status_code in (400, 401)
    assert await live_db.count("spans") == 0  # the first batch went with the Project


@pytest.mark.pg
async def test_database_outage_then_recovery_within_10_s(
    sqlite_template: Path, tmp_path: Path
) -> None:
    """REQ-330: 503 while the database is unreachable, back to 200 within 10 s, no restart."""
    async with database("pg", sqlite_template, tmp_path) as db:
        await db.seed()
        pg = db.url.split("@", 1)[1].split("/", 1)[0]
        host, port = pg.rsplit(":", 1)
        proxy = TcpProxy(host, int(port))
        await proxy.start()
        async with live_server(db, tmp_path, db_url=proxy.url_for(db.url)) as srv:
            assert (await srv.post(body(1, trace=1))).status_code == 200
            await proxy.cut()
            down = await srv.post(body(1, trace=2))
            assert down.status_code == 503
            assert down.headers["retry-after"] in ("2", "5")
            assert (await srv.post(body(1, trace=3))).status_code == 503
            await proxy.restore()
            restored = time.monotonic()
            while True:
                resp = await srv.post(body(1, trace=4))
                if resp.status_code == 200:
                    break
                assert resp.status_code == 503
                assert time.monotonic() - restored < 10, "no recovery within 10 s"
                await asyncio.sleep(0.25)
        await proxy.cut()
        assert await db.count("spans") == 2
