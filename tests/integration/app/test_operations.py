"""Graceful shutdown and rebuild under ingest (plan tasks P5-05.1, P5-06.1; REQ-331.a, 042.b).

Both run the real server code in-process on both backends: ``run_server`` on a real port (its
``stop`` event is what the SIGTERM handler sets; ``test_server_process`` covers the signal itself)
and the lifespan-driven app for the rebuild.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
from typing import Any

import httpx
import pytest
from opentelemetry.proto.trace.v1.trace_pb2 import Span
from sqlalchemy import text

from reflex_agentlens.app.backend import open_backend
from reflex_agentlens.app.bootstrap import KEY_FILE
from reflex_agentlens.app.server import run_server
from reflex_agentlens.ingest.rederive import rederive
from reflex_agentlens.ingest.writer import WriteQueue
from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.records import NormalizedSpan, PricingVersion, RebuildScope
from reflex_agentlens.store.sqlite.backend import SqliteBackend
from tests.contract.ingest_rig import now_request
from tests.contract.snapshot import snapshot
from tests.integration.app.conftest import Fresh
from tests.integration.app.test_runtime import running
from tests.integration.live import free_port
from tests.unit.ingest import otlp

PB = "application/x-protobuf"


def agent_trace(trace: int, late: bool = False) -> list[Span]:
    """Root agent span and one LLM call; ``late`` adds a further LLM call to the same trace."""
    b = trace * 10
    llm = {
        "gen_ai__operation__name": "chat",
        "gen_ai__provider__name": "openai",
        "gen_ai__request__model": "gpt-4o",
        "gen_ai__usage__input_tokens": 100 + trace,
        "gen_ai__usage__output_tokens": 20,
    }
    if late:
        return [otlp.span(trace, b + 5, b + 1, 4, 3, name="chat gpt-4o", **llm)]
    return [
        otlp.span(
            trace, b + 1, None, 10, 2, name="invoke_agent support",
            gen_ai__operation__name="invoke_agent", gen_ai__agent__name="support",
        ),
        otlp.span(trace, b + 2, b + 1, 9, 7, name="chat gpt-4o", **llm),
    ]  # fmt: skip


def body(spans: list[Span]) -> bytes:
    return now_request(*spans).SerializeToString()


def read_key(fresh: Fresh) -> str:
    return (fresh.tmp_path / "data" / KEY_FILE).read_text().strip()


async def wait_ready(client: httpx.AsyncClient, server: asyncio.Task[None]) -> None:
    for _ in range(300):
        if server.done():
            server.result()  # raise the startup error
        try:
            if (await client.get("/readyz")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("server not ready")


async def test_graceful_shutdown_commits_in_flight_requests_req_331a(
    fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_cls: Any = SqliteBackend if fresh.name == "sqlite" else PostgresBackend
    original = store_cls.write_group

    async def slow_write_group(self: Any, batches: Any) -> Any:
        await asyncio.sleep(2.0)  # keep the requests in flight when the stop arrives
        return await original(self, batches)

    monkeypatch.setattr(store_cls, "write_group", slow_write_group)
    queued: list[int] = []  # appended from the ingest thread (list.append is atomic)
    original_put = WriteQueue.put

    def counting_put(self: WriteQueue, batch: Any, size: int | None = None) -> Any:
        future = original_put(self, batch, size)
        queued.append(1)
        return future

    monkeypatch.setattr(WriteQueue, "put", counting_put)
    port = free_port()
    cfg = fresh.config(**{"server.http_port": port, "server.shutdown_grace": "10s"})
    stop = asyncio.Event()
    server = asyncio.create_task(run_server(cfg, "serve", banner=io.StringIO(), stop=stop))
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
        await wait_ready(client, server)
        headers = {"content-type": PB, "authorization": f"Bearer {read_key(fresh)}"}
        in_flight = [
            asyncio.create_task(
                client.post("/v1/traces", content=body(agent_trace(t)), headers=headers)
            )
            for t in range(1, 6)
        ]
        for _ in range(400):  # every batch admitted and queued, none committed yet
            if len(queued) == 5:
                break
            await asyncio.sleep(0.025)
        assert len(queued) == 5
        stop.set()  # what SIGTERM does
        await asyncio.sleep(0.05)
        late = await client.post("/v1/traces", content=body(agent_trace(99)), headers=headers)
        assert late.status_code == 503  # admission closed
        health = await client.get("/healthz")
        assert (health.status_code, health.json()) == (503, {"status": "shutting_down"})
        responses = await asyncio.gather(*in_flight)
        assert [r.status_code for r in responses] == [200] * 5
    await asyncio.wait_for(server, 15)  # returns normally: the CLI exits 0

    backend = await open_backend(cfg)
    try:
        async with backend.write_engine.connect() as conn:
            stored = (await conn.execute(text("SELECT count(*) FROM spans"))).scalar_one()
    finally:
        await backend.close()
    assert stored == 10  # 5 traces x 2 spans; trace 99 never got in


def strip(span: NormalizedSpan, _: PricingVersion, /) -> NormalizedSpan:
    """A stale derivation: what an older mapping table might have produced."""
    return dataclasses.replace(
        span, category="generic", is_genai=False, llm_call=None, agent_name=None,
        agent_usage_input=None, agent_usage_output=None, search_doc=None,
    )  # fmt: skip


async def test_rebuild_under_concurrent_ingest_req_042b(fresh: Fresh) -> None:
    traces = range(1, 41)
    async with running(fresh, fresh.config(), banner=io.StringIO()) as (handle, client):
        rt = handle.runtime
        assert rt is not None
        headers = {"content-type": PB, "authorization": f"Bearer {read_key(fresh)}"}
        for t in traces:
            response = await client.post(
                "/v1/traces", content=body(agent_trace(t)), headers=headers
            )
            assert response.status_code == 200
        backend = rt.backend
        pricing = await backend.get_pricing()
        await backend.rebuild(RebuildScope(), strip, pricing)  # make every Run stale

        async def late_spans() -> None:
            for t in traces:
                late = await client.post(
                    "/v1/traces", content=body(agent_trace(t, late=True)), headers=headers
                )
                assert late.status_code == 200

        report, _ = await asyncio.gather(
            backend.rebuild(RebuildScope(), rederive, pricing, batch_runs=3), late_spans()
        )
        assert report.runs_changed > 0  # REQ-042.a: Runs changed are reported

        (project,) = await backend.list_projects()

        async def views() -> dict[str, Any]:
            async with backend.write_engine.connect() as conn:
                return await conn.run_sync(snapshot, project.id)

        during = await views()
        again = await backend.rebuild(RebuildScope(), rederive, pricing)  # ingest is idle now
        assert again.runs_changed == 0  # identical to a rebuild with ingest stopped
        assert await views() == during
        llm_calls = during["llm_calls"]
        assert len(llm_calls) == 2 * len(traces)  # the first call + the late one per trace
