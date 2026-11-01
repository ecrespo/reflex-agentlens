"""Topology equivalence (plan task P5-08.1; REQ-004.c).

The golden OTLP fixtures (P3-12/P3-13) go, byte for byte, through the ``serve`` app and through
the ``collect`` app (PostgreSQL), each into its own Project of one database. The stored data must
be identical: the export responses, and every derived view keyed by natural keys.
"""

from __future__ import annotations

import io
import json
import time
from typing import Any

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from reflex_agentlens.app.backend import Backend
from reflex_agentlens.ingest.decode import decode_traces
from reflex_agentlens.query import dto
from tests.contract.snapshot import snapshot
from tests.golden.test_golden import FIXTURES, jsonable
from tests.integration.app.conftest import Fresh
from tests.integration.app.test_operations import PB
from tests.integration.app.test_runtime import running
from tests.unit.ingest import otlp

ADMIN = dto.Viewer("topology", "topology", is_admin=True)


def shifted(doc: dict[str, Any], shift: int) -> bytes:
    """One fixture request as protobuf, times moved from the fixed clock to now."""
    request = decode_traces(json.dumps(doc).encode(), "json")
    assert isinstance(request, ExportTraceServiceRequest)
    for rs in request.resource_spans:
        for ss in rs.scope_spans:
            for sp in ss.spans:
                sp.start_time_unix_nano += shift
                sp.end_time_unix_nano += shift
                for ev in sp.events:
                    ev.time_unix_nano += shift
    return request.SerializeToString()


async def views(backend: Backend, project_id: str) -> dict[str, Any]:
    async with backend.write_engine.connect() as conn:
        return jsonable(await conn.run_sync(snapshot, project_id))


async def send(client: httpx.AsyncClient, key: str, body: bytes) -> tuple[int, bytes]:
    headers = {"content-type": PB, "authorization": f"Bearer {key}"}
    response = await client.post("/v1/traces", content=body, headers=headers)
    return response.status_code, response.content


async def test_serve_and_collect_store_identical_data_req_004c(fresh: Fresh) -> None:
    if fresh.name != "pg":
        pytest.skip("collect runs on PostgreSQL only (REQ-004.b)")
    shift = time.time_ns() - otlp.NOW_NS
    fixtures = [(p.stem, json.loads(p.read_text())) for p in FIXTURES]
    assert fixtures
    async with running(fresh, fresh.config(), banner=io.StringIO()) as (serve, serve_client):
        rt = serve.runtime
        assert rt is not None
        assert rt.admin is not None
        pairs: list[tuple[str, str, str, str, str]] = []  # name, serve pid/key, collect pid/key
        for i, (name, _) in enumerate(fixtures):
            ids_keys = []
            for side in ("s", "c"):
                project = await rt.admin.create_project(ADMIN, f"{side}{name}", f"{side}-g{i}")
                key = await rt.admin.create_api_key(ADMIN, project.id)
                ids_keys += [project.id, key.plaintext.get_secret_value()]
            pairs.append((name, *ids_keys))  # type: ignore[arg-type]

        async with running(fresh, fresh.config(), "collect") as (collect, collect_client):
            assert collect.runtime is not None
            assert collect.runtime.query is None  # a collector serves no UI
            for (name, _, serve_key, _, collect_key), (_, fixture) in zip(
                pairs, fixtures, strict=True
            ):
                for doc in fixture["requests"]:
                    body = shifted(doc, shift)
                    via_serve = await send(serve_client, serve_key, body)
                    via_collect = await send(collect_client, collect_key, body)
                    assert via_serve == via_collect, name
                    assert via_serve[0] == 200, name

        for name, serve_pid, _, collect_pid, _ in pairs:
            expected = await views(rt.backend, serve_pid)
            assert expected["runs"], name
            assert await views(rt.backend, collect_pid) == expected, name
