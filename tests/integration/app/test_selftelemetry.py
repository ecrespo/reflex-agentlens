"""Self-telemetry through the real app (plan task P5-07.1; REQ-342.a, 323.a).

An in-memory reader and span exporter stand in for the OTLP exporters. The payload carries canary
values everywhere a user can put text; none of them may appear in the exported data.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from reflex_agentlens.app.selftelemetry import OtelRecorder
from reflex_agentlens.core import telemetry
from reflex_agentlens.query import dto
from tests.contract.ingest_rig import now_request
from tests.integration.app.conftest import Fresh
from tests.integration.app.test_operations import PB, read_key
from tests.integration.app.test_runtime import running
from tests.unit.ingest import otlp

CANARY = "CANARY-7f3e"
ADMIN = dto.Viewer("t", "t", is_admin=True)


class Captured:
    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.spans = InMemorySpanExporter()
        tracers = TracerProvider()
        tracers.add_span_processor(SimpleSpanProcessor(self.spans))
        self.meters = MeterProvider(metric_readers=[self.reader])
        self.recorder = OtelRecorder(self.meters, tracers)

    def points(self) -> dict[str, list[tuple[dict[str, Any], Any]]]:
        """metric name → [(attributes, value or histogram count)]."""
        data = self.reader.get_metrics_data()
        out: dict[str, list[tuple[dict[str, Any], Any]]] = {}
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    for p in metric.data.data_points:
                        value = getattr(p, "value", None)
                        if value is None:
                            value = getattr(p, "count", 0)
                        attrs = dict(p.attributes or {})
                        out.setdefault(metric.name, []).append((attrs, value))
        return out

    def dump(self) -> str:
        data = self.reader.get_metrics_data()
        spans = [s.to_json() for s in self.spans.get_finished_spans()]
        return (data.to_json() if data else "") + json.dumps(spans)


@pytest.fixture
def captured() -> Iterator[Captured]:
    cap = Captured()
    telemetry.install(cap.recorder)
    try:
        yield cap
    finally:
        telemetry.install(None)


def canary_request() -> bytes:
    spans = [
        otlp.span(
            1, 11, None, 10, 2, name=f"invoke_agent {CANARY}",
            gen_ai__operation__name="invoke_agent", gen_ai__agent__name=CANARY,
        ),
        otlp.span(
            1, 12, 11, 9, 7, name=f"chat {CANARY}",
            gen_ai__operation__name="chat", gen_ai__provider__name="openai",
            gen_ai__request__model=CANARY, gen_ai__usage__input_tokens=10,
            gen_ai__input__messages=f'[{{"role":"user","parts":[{{"type":"text","content":"{CANARY}"}}]}}]',
        ),
    ]  # fmt: skip
    return now_request(*spans).SerializeToString()


def total(points: list[tuple[dict[str, Any], Any]], **labels: str) -> Any:
    return sum(v for attrs, v in points if all(attrs.get(k) == x for k, x in labels.items()))


async def test_exports_the_design_metrics_and_spans_without_content_req_342a_323a(
    fresh: Fresh, captured: Captured
) -> None:
    async with running(fresh, fresh.config(), banner=io.StringIO()) as (handle, client):
        rt = handle.runtime
        assert rt is not None
        assert rt.query is not None
        headers = {"content-type": PB, "authorization": f"Bearer {read_key(fresh)}"}
        ok = await client.post("/v1/traces", content=canary_request(), headers=headers)
        again = await client.post("/v1/traces", content=canary_request(), headers=headers)
        denied = await client.post(
            "/v1/traces", content=canary_request(), headers={"content-type": PB}
        )
        assert (ok.status_code, again.status_code, denied.status_code) == (200, 200, 401)
        assert await rt.query.list_projects(ADMIN)

    points = captured.points()
    requests = points["agentlens.ingest.requests"]
    assert total(requests, protocol="http/protobuf", outcome="accepted") == 2
    assert total(requests, outcome="rejected_auth") == 1
    spans = points["agentlens.ingest.spans"]
    assert total(spans, outcome="accepted") == 2
    assert total(spans, outcome="duplicate") == 2
    assert total(points["agentlens.write.commit.duration"]) >= 1
    assert total(points["agentlens.write.group.spans"]) >= 1
    assert "agentlens.ingest.queue.bytes" in points
    assert total(points["agentlens.query.duration"], query="list_projects") == 1

    names = {s.name for s in captured.spans.get_finished_spans()}
    assert {"agentlens.ingest.export", "agentlens.write.commit", "agentlens.query"} <= names
    for span in captured.spans.get_finished_spans():
        assert set(span.attributes or {}) <= telemetry.SPANS[span.name]

    assert CANARY not in captured.dump()  # REQ-323.a: no ingested value leaves the process


def test_undeclared_labels_never_leak(captured: Captured) -> None:
    rec = captured.recorder
    rec.add(
        "agentlens.ingest.requests", 1, {"protocol": CANARY, "outcome": "accepted", "x": CANARY}
    )
    rec.add(f"agentlens.{CANARY}", 1)  # undeclared metric: dropped
    with rec.span("agentlens.query", {"query": CANARY}):
        pass
    with rec.span(CANARY):  # undeclared span: not recorded
        pass
    ((attrs, _),) = captured.points()["agentlens.ingest.requests"]
    assert attrs == {"protocol": "other", "outcome": "accepted"}
    (span,) = captured.spans.get_finished_spans()
    assert dict(span.attributes or {}) == {"query": "other"}
    assert CANARY not in captured.dump()
