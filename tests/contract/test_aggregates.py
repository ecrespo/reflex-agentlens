"""Dashboard aggregate contract tests (plan task P2-07.3; Data Model Q8, Q10; REQ-106, DD-005).

Every expected value is computed in Python from the seeded dataset (or from the Runs as stored),
so both backends must return exactly the same numbers. REQ-106.h is checked with the
rows-fetched recorder: no aggregate statement may return more rows than its groups.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable

import pytest

from reflex_agentlens.store.dialect import percentile_rank
from reflex_agentlens.store.records import (
    DAY_NS,
    HOUR_NS,
    MINUTE_NS,
    AggQuery,
    NormalizedSpan,
    RunQuery,
    RunSummary,
    SeriesPoint,
    TimeRange,
    auto_bucket_ns,
)
from tests.contract.conftest import ContractDB
from tests.contract.dataset import BASE_NS, SOURCES, dataset
from tests.contract.rowcount import rows_fetched
from tests.contract.test_reads import all_runs, load

RUNS = dataset(5, 3000)
SPANS = [sp for run in RUNS for sp in run]
WINDOW = TimeRange(BASE_NS, BASE_NS + 31 * DAY_NS)


def nearest(values: Iterable[int], p: float) -> int | None:
    ordered = sorted(values)
    return ordered[percentile_rank(p, len(ordered)) - 1] if ordered else None


async def setup(db: ContractDB) -> tuple[AggQuery, list[RunSummary]]:
    project = await load(db, RUNS)
    q = AggQuery(frozenset({project}), WINDOW)
    return q, await all_runs(db, RunQuery(frozenset({project})))


def test_auto_bucket() -> None:
    """REQ-106: minute, hour or day buckets by range length."""
    assert auto_bucket_ns(TimeRange(0, HOUR_NS)) == MINUTE_NS
    assert auto_bucket_ns(TimeRange(0, 2 * HOUR_NS)) == MINUTE_NS
    assert auto_bucket_ns(TimeRange(0, DAY_NS)) == HOUR_NS
    assert auto_bucket_ns(TimeRange(0, 7 * DAY_NS)) == DAY_NS
    assert AggQuery(frozenset(), TimeRange(0, 7 * DAY_NS), bucket_ns=5).bucket == 5


async def test_kpis(db: ContractDB) -> None:
    """REQ-106.g: totals, error count, priced cost with unpriced count, p95, active Sessions."""
    q, runs = await setup(db)
    k = await db.require_backend().agg_kpis(q)
    assert k.runs == len(runs)
    assert k.error_runs == sum(r.status == "error" for r in runs)
    assert k.input_tokens == sum(r.input_tokens for r in runs)
    assert k.output_tokens == sum(r.output_tokens for r in runs)
    assert k.total_tokens == k.input_tokens + k.output_tokens
    assert k.cost_nusd == sum(r.cost_nusd or 0 for r in runs)
    assert k.unpriced_runs == sum(r.unpriced_calls > 0 for r in runs)
    assert k.includes_unpriced
    assert k.p95_duration_ns == nearest((r.duration_ns for r in runs if r.duration_ns), 0.95)
    assert k.active_sessions == len({r.session_id for r in runs if r.session_id})


async def test_runs_over_time(db: ContractDB) -> None:
    """REQ-106.a: Run count per (day bucket, status)."""
    q, runs = await setup(db)
    ts = await db.require_backend().agg_runs_over_time(q)
    assert ts.bucket_ns == DAY_NS
    counts = Counter(((r.start_ns // DAY_NS) * DAY_NS, r.status) for r in runs)
    assert ts.points == sorted(SeriesPoint(b, (st,), n) for (b, st), n in counts.items())


async def test_tokens_cost_by_model(db: ContractDB) -> None:
    """REQ-106.b with REQ-061.c: totals and per-bucket rows flag unpriced calls."""
    q, _ = await setup(db)
    usage = await db.require_backend().agg_tokens_cost_by_model(q)
    calls = [sp.llm_call for sp in SPANS if sp.llm_call]
    by_model: dict[str, list[int]] = defaultdict(lambda: [0] * 6)
    for c in calls:
        row = by_model[c.model]
        row[0] += 1
        row[1] += c.input_tokens or 0
        row[2] += c.output_tokens or 0
        row[3] += c.cached_input_tokens or 0
        row[4] += c.cost_nusd or 0
        row[5] += c.unpriced
    got = {
        x.model: [x.calls, x.input_tokens, x.output_tokens, x.cached_input_tokens, x.cost_nusd,
                  x.unpriced_calls]
        for x in usage.totals
    }  # fmt: skip
    assert got == dict(by_model)
    assert all(x.bucket_ns is None for x in usage.totals)
    assert {x.model for x in usage.totals if x.includes_unpriced} == {"llama3.3:70b"}
    per_bucket: Counter[tuple[int, str]] = Counter()
    for c in calls:
        per_bucket[((c.start_ns // DAY_NS) * DAY_NS, c.model)] += c.input_tokens or 0
    assert {(x.bucket_ns, x.model): x.input_tokens for x in usage.over_time} == dict(per_bucket)
    assert sum(x.calls for x in usage.over_time) == len(calls)


def _latency_expected(spans: list[NormalizedSpan], by: str) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for sp in spans:
        if by == "operation":
            if sp.llm_call:
                groups[sp.llm_call.operation].append(sp.llm_call.duration_ns)
            if sp.tool_call:
                groups["execute_tool"].append(sp.tool_call.duration_ns)
            if sp.graph_node and sp.graph_node.node_kind == "agent":
                groups["invoke_agent"].append(sp.graph_node.end_ns - sp.graph_node.start_ns)
        elif sp.graph_node:
            groups[sp.graph_node.node_name].append(sp.graph_node.end_ns - sp.graph_node.start_ns)
    return groups


@pytest.mark.parametrize("by", ["operation", "node"])
async def test_latency_percentiles(db: ContractDB, by: str) -> None:
    """REQ-106.c: nearest-rank p50/p95/p99 per operation and per graph node (DD-005)."""
    q, _ = await setup(db)
    rows = await db.require_backend().agg_latency_percentiles(q, by)  # type: ignore[arg-type]
    expected = _latency_expected(SPANS, by)
    assert [x.group for x in rows] == sorted(expected)
    for x in rows:
        values = expected[x.group]
        assert x.count == len(values)
        assert (x.p50_ns, x.p95_ns, x.p99_ns) == (
            nearest(values, 0.5), nearest(values, 0.95), nearest(values, 0.99),
        )  # fmt: skip


async def test_tool_stats_top_n(db: ContractDB) -> None:
    """REQ-106.d: calls, errors and success per tool; top N by calls, ties by name."""
    q, _ = await setup(db)
    calls = Counter(sp.tool_call.tool_name for sp in SPANS if sp.tool_call)
    errors = Counter(
        sp.tool_call.tool_name for sp in SPANS if sp.tool_call and sp.tool_call.status == "error"
    )
    ranked = sorted(calls, key=lambda t: (-calls[t], t))
    backend = db.require_backend()
    for n in (1, 3, 100):
        rows = await backend.agg_tool_stats(q, n)
        assert [x.tool_name for x in rows] == ranked[:n]
        assert all((x.calls, x.errors) == (calls[x.tool_name], errors[x.tool_name]) for x in rows)
        assert all(x.successes == x.calls - x.errors for x in rows)
    assert await backend.agg_tool_stats(q, 0) == []


async def test_by_source(db: ContractDB) -> None:
    """REQ-106.e: cost and tokens per Source of the Runs."""
    q, runs = await setup(db)
    rows = await db.require_backend().agg_by_source(q)
    expected: dict[object, list[int]] = defaultdict(lambda: [0] * 5)
    for r in runs:
        e = expected[r.source]
        e[0] += 1
        e[1] += r.input_tokens
        e[2] += r.output_tokens
        e[3] += r.cost_nusd or 0
        e[4] += r.unpriced_calls > 0
    got = {
        x.source: [x.runs, x.input_tokens, x.output_tokens, x.cost_nusd, x.unpriced_runs]
        for x in rows
    }
    assert got == dict(expected)


async def test_approvals(db: ContractDB) -> None:
    """REQ-106.f: count per decision and the median wait."""
    q, _ = await setup(db)
    stats = await db.require_backend().agg_approvals(q)
    approvals = [sp.approval for sp in SPANS if sp.approval]
    assert approvals
    by_decision: dict[str, list[int]] = defaultdict(list)
    for a in approvals:
        by_decision[a.decision].append(a.wait_ns or 0)
    assert [(x.decision, x.count, x.median_wait_ns) for x in stats.by_decision] == [
        (d, len(w), nearest(w, 0.5)) for d, w in sorted(by_decision.items())
    ]
    assert stats.median_wait_ns == nearest((a.wait_ns or 0 for a in approvals), 0.5)


async def test_source_and_time_scope(db: ContractDB) -> None:
    """REQ-106 scope: Source filter (Run source / span source) and half-open time range."""
    q, runs = await setup(db)
    backend = db.require_backend()
    src = SOURCES[0]
    scoped = AggQuery(q.project_ids, WINDOW, sources=(src,))
    k = await backend.agg_kpis(scoped)
    assert k.runs == sum(r.source == src for r in runs)
    tools = await backend.agg_tool_stats(scoped, 100)
    expected = Counter(sp.tool_call.tool_name for sp in SPANS if sp.tool_call and sp.source == src)
    assert {x.tool_name: x.calls for x in tools} == dict(expected)

    half = TimeRange(BASE_NS, BASE_NS + 15 * DAY_NS)
    k = await backend.agg_kpis(AggQuery(q.project_ids, half))
    assert k.runs == sum(half.start_ns <= r.start_ns < half.end_ns for r in runs)

    unknown = AggQuery(q.project_ids, WINDOW, sources=(SOURCES[0].__class__("nope"),))
    assert (await backend.agg_kpis(unknown)).runs == 0
    empty = AggQuery(frozenset(), WINDOW)
    assert (await backend.agg_kpis(empty)).runs == 0
    assert (await backend.agg_runs_over_time(empty)).points == []
    assert (await backend.agg_tokens_cost_by_model(empty)).totals == []
    assert await backend.agg_latency_percentiles(empty, "node") == []
    assert await backend.agg_by_source(empty) == []
    assert (await backend.agg_approvals(empty)).by_decision == []
    nothing = AggQuery(q.project_ids, TimeRange(BASE_NS - DAY_NS, BASE_NS))
    k = await backend.agg_kpis(nothing)
    assert (k.runs, k.p95_duration_ns, k.cost_nusd) == (0, None, 0)
    assert (await backend.agg_approvals(nothing)).median_wait_ns is None


async def test_aggregates_fetch_bounded_rows(db: ContractDB) -> None:
    """REQ-106.h: every aggregate statement returns at most (buckets x groups) rows."""
    q, runs = await setup(db)
    backend = db.require_backend()
    engine = backend.read_engine  # type: ignore[attr-defined]
    assert len(runs) > 200
    with rows_fetched(engine) as rec:
        await backend.agg_kpis(q)
        await backend.agg_runs_over_time(q)
        await backend.agg_tokens_cost_by_model(q)
        await backend.agg_latency_percentiles(q, "operation")
        await backend.agg_latency_percentiles(q, "node")
        await backend.agg_tool_stats(q, 10)
        await backend.agg_by_source(q)
        await backend.agg_approvals(q)
    assert rec.statements, "the recorder saw no statement"
    # 31 day buckets x 5 models is the largest legitimate result.
    rec.assert_bounded(31 * 5)
