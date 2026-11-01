"""Dashboard, storage status and graph through ``QueryService`` (plan tasks P4-03.1, P4-04.1).

REQ-106/107 (values equal the store aggregates), REQ-130.d (a failing aggregate leaves the
others filled), REQ-173 (storage status), REQ-153 (graph on a stored Run).
"""

from __future__ import annotations

from typing import Any

import pytest

from reflex_agentlens.core.errors import Forbidden, StorageUnavailable
from reflex_agentlens.core.timeutil import ns_to_rfc3339
from reflex_agentlens.query import convert, dto
from reflex_agentlens.query.filters import RunFilter
from reflex_agentlens.query.service import TOP_TOOLS, QueryService
from reflex_agentlens.store.records import DAY_NS, AggQuery, NormalizedBatch, TimeRange
from tests.contract.conftest import ContractDB
from tests.contract.dataset import BASE_NS, dataset

RUNS = dataset(9, 800)
KEY = b"d" * 32
ADMIN = dto.Viewer("admin", "Admin", is_admin=True)
START, END = BASE_NS, BASE_NS + 31 * DAY_NS


def scope(**kw: Any) -> dto.AggScope:
    return dto.AggScope(time_from=ns_to_rfc3339(START), time_to=ns_to_rfc3339(END), **kw)


async def setup(db: ContractDB) -> tuple[QueryService, str]:
    project = await db.make_project("dash")
    spans = tuple(sp for run in RUNS for sp in run)
    await db.require_backend().write_batch(NormalizedBatch(project, spans))
    return QueryService(
        db.require_backend(), KEY, clock_ns=db.clock, size_limit_bytes=10**9
    ), project


async def test_dashboard_equals_store_aggregates_req_106(db: ContractDB) -> None:
    q, project = await setup(db)
    data = await q.dashboard(ADMIN, scope(project_ids=["dash"]))
    store = db.require_backend()
    agg = AggQuery(frozenset({project}), TimeRange(START, END))
    assert data.failed == []
    assert data.project_ids == [project]
    assert data.bucket_ns == agg.bucket
    assert data.kpis == convert.kpis(await store.agg_kpis(agg))
    assert data.kpis is not None
    assert data.kpis.runs == len(RUNS)
    assert data.runs_over_time == convert.time_series(await store.agg_runs_over_time(agg))
    assert data.model_usage == convert.model_usage(await store.agg_tokens_cost_by_model(agg))
    assert data.latency_by_operation == convert.latency_rows(
        await store.agg_latency_percentiles(agg, "operation")
    )
    assert data.latency_by_node == convert.latency_rows(
        await store.agg_latency_percentiles(agg, "node")
    )
    assert data.tools == convert.tool_stats(await store.agg_tool_stats(agg, TOP_TOOLS))
    assert data.by_source == convert.source_usage(await store.agg_by_source(agg))
    assert data.approvals == convert.approval_stats(await store.agg_approvals(agg))
    assert data.metric_tokens == convert.time_series(await store.agg_metric_token_usage(agg))
    assert data.model_usage is not None
    assert any(row.includes_unpriced for row in data.model_usage.totals)  # REQ-061.c


async def test_dashboard_bucket_and_empty_scope(db: ContractDB) -> None:
    q, _ = await setup(db)
    hourly = await q.dashboard(ADMIN, scope(bucket="hour"))
    assert hourly.bucket_ns == 3600 * 10**9
    nobody = dto.Viewer("v", "V", is_admin=False, allowed_project_ids=frozenset())
    empty = await q.dashboard(nobody, scope())
    assert (empty.project_ids, empty.kpis, empty.failed) == ([], None, [])


async def test_one_failing_aggregate_leaves_the_rest_req_130d(
    db: ContractDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    q, _ = await setup(db)

    async def boom(*_: object) -> None:
        raise StorageUnavailable("lost")

    monkeypatch.setattr(db.require_backend(), "agg_tool_stats", boom)
    data = await q.dashboard(ADMIN, scope())
    assert data.failed == ["tools"]
    assert data.tools is None
    assert data.kpis is not None
    for name in (
        "agg_kpis", "agg_runs_over_time", "agg_tokens_cost_by_model", "agg_latency_percentiles",
        "agg_by_source", "agg_approvals", "agg_metric_token_usage",
    ):  # fmt: skip
        monkeypatch.setattr(db.require_backend(), name, boom)
    with pytest.raises(StorageUnavailable):
        await q.dashboard(ADMIN, scope())


async def test_storage_status_req_173(db: ContractDB) -> None:
    q, project = await setup(db)
    status = await q.storage_status(ADMIN)
    assert status.runs == len(RUNS)
    assert status.spans == sum(len(run) for run in RUNS)
    assert status.size_limit_bytes == 10**9
    assert status.last_purge is None
    limited = dto.Viewer("v", "V", is_admin=False, allowed_project_ids=frozenset({project}))
    with pytest.raises(Forbidden):
        await q.storage_status(limited)


async def test_graph_on_a_stored_run_req_153(db: ContractDB) -> None:
    q, _ = await setup(db)
    run = (await q.list_runs(ADMIN, RunFilter(), "tokens_desc", limit=1)).items[0]
    detail = await q.get_trace(ADMIN, run.ref)
    assert detail.graph is not None
    span_ids = {n.span_id for n in detail.spans}
    for node in detail.graph.nodes:
        assert set(node.span_ids) <= span_ids
    ids = {n.id for n in detail.graph.nodes}
    assert all(e.source in ids and e.target in ids for e in detail.graph.edges)
    assert await q.get_graph(ADMIN, run.ref, full=True) == detail.graph  # small: same view
