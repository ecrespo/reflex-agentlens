"""Cross-backend parity (plan task P2-11.1; REQ-070.b, REQ-072.a, REQ-073.b, REQ-074).

The same dataset is written to SQLite and PostgreSQL and every read must agree. Results are
compared on **natural keys only** (trace IDs, Session external IDs, Source keys): Project and
Session ULIDs are generated per database, so they are dropped before comparing. Search compares
result *sets* (REQ-073.b), with the DD-006 normalization applied by both backends.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from reflex_agentlens.store.records import (
    AggQuery,
    RunQuery,
    RunSort,
    SessionQuery,
    TimeRange,
)
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.dataset import BASE_NS, NS, dataset
from tests.contract.test_reads import all_runs, load
from tests.contract.test_sessions import all_sessions

pytestmark = pytest.mark.pg

RUNS = dataset(31, 2500)
DAY = 86_400 * NS
SURROGATES = frozenset({"project_id", "session_id", "id"})
Pair = tuple[ContractDB, ContractDB]


def natural(value: Any) -> Any:
    """Drop surrogate IDs recursively so both databases' results compare equal."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: natural(v) for k, v in value.items() if k not in SURROGATES}
    if isinstance(value, (list, tuple)):
        return [natural(v) for v in value]
    return value


async def loaded(pair: Pair) -> tuple[str, str]:
    lite, pg = pair
    return await load(lite, RUNS), await load(pg, RUNS)


@pytest.mark.parametrize(
    "sort",
    ["start_desc", "start_asc", "duration_desc", "tokens_asc", "cost_desc", "cost_asc"],
)
async def test_run_lists_match(pair: Pair, sort: RunSort) -> None:
    """REQ-070.b: same Runs, same order, same totals; pages walked with each backend's cursor."""
    (lite, pg), (a, b) = pair, await loaded(pair)
    got_lite = await all_runs(lite, RunQuery(frozenset({a}), sort=sort), page=37)
    got_pg = await all_runs(pg, RunQuery(frozenset({b}), sort=sort), page=37)
    assert len(got_lite) == len(RUNS)
    assert natural(got_lite) == natural(got_pg)


async def test_filtered_run_lists_match(pair: Pair) -> None:
    (lite, pg), (a, b) = pair, await loaded(pair)
    window = TimeRange(BASE_NS + 5 * DAY, BASE_NS + 20 * DAY)
    for q in (
        RunQuery(frozenset(), statuses=("error",)),
        RunQuery(frozenset(), time_range=window, is_genai=True),
        RunQuery(frozenset(), models=("gpt-4o",), sort="tokens_desc"),
    ):
        got_lite = await all_runs(lite, dataclasses.replace(q, project_ids=frozenset({a})))
        got_pg = await all_runs(pg, dataclasses.replace(q, project_ids=frozenset({b})))
        assert natural(got_lite) == natural(got_pg), q


@pytest.mark.parametrize(
    "text",
    ["refund", "REFUND", "Refund policy", "tool", "réfund", "weather report", "x", '"refund"'],
)
async def test_search_result_sets_match(pair: Pair, text: str) -> None:
    """REQ-073.b: identical result sets for the same normalized query (DD-006)."""
    (lite, pg), (a, b) = pair, await loaded(pair)
    hits_lite = await all_runs(lite, RunQuery(frozenset({a}), text=text))
    hits_pg = await all_runs(pg, RunQuery(frozenset({b}), text=text))
    assert {r.trace_id for r in hits_lite} == {r.trace_id for r in hits_pg}


async def test_aggregates_and_percentiles_match(pair: Pair) -> None:
    """REQ-074 / DD-005: nearest-rank percentiles are exact on both backends."""
    (lite, pg), (a, b) = pair, await loaded(pair)
    lb, pb = lite.require_backend(), pg.require_backend()
    for window in (
        TimeRange(BASE_NS, NOW_NS),
        TimeRange(BASE_NS + 3 * DAY, BASE_NS + 4 * DAY),
        TimeRange(BASE_NS + 3 * DAY, BASE_NS + 3 * DAY + 3_600 * NS),
    ):
        qa, qb = AggQuery(frozenset({a}), window), AggQuery(frozenset({b}), window)
        for by in ("operation", "node"):
            assert natural(await lb.agg_latency_percentiles(qa, by)) == natural(
                await pb.agg_latency_percentiles(qb, by)
            ), (window, by)
        assert natural(await lb.agg_kpis(qa)) == natural(await pb.agg_kpis(qb))
        assert natural(await lb.agg_runs_over_time(qa)) == natural(await pb.agg_runs_over_time(qb))
        assert natural(await lb.agg_tokens_cost_by_model(qa)) == natural(
            await pb.agg_tokens_cost_by_model(qb)
        )
        assert natural(await lb.agg_tool_stats(qa, 10)) == natural(await pb.agg_tool_stats(qb, 10))
        assert natural(await lb.agg_by_source(qa)) == natural(await pb.agg_by_source(qb))
        assert natural(await lb.agg_approvals(qa)) == natural(await pb.agg_approvals(qb))


async def test_sessions_match(pair: Pair) -> None:
    (lite, pg), (a, b) = pair, await loaded(pair)
    for sort in ("last_desc", "cost_desc", "runs_desc"):
        got_lite = await all_sessions(lite, SessionQuery(frozenset({a}), sort=sort), page=23)
        got_pg = await all_sessions(pg, SessionQuery(frozenset({b}), sort=sort), page=23)
        assert got_lite
        # ties are broken by the Session ULID (API SessionCursor), a surrogate generated per
        # database: the sort keys must agree position by position, tied rows as a set
        attr = {"last_desc": "last_ns", "cost_desc": "cost_nusd", "runs_desc": "run_count"}[sort]
        assert [getattr(x, attr) for x in got_lite] == [getattr(x, attr) for x in got_pg]
        assert sorted(natural(got_lite), key=repr) == sorted(natural(got_pg), key=repr), sort
    first = got_lite[0].external_id
    lite_ids = {s.external_id: s.session_id for s in got_lite}
    pg_ids = {s.external_id: s.session_id for s in got_pg}
    detail_lite = await lite.require_backend().get_session(frozenset({a}), lite_ids[first])
    detail_pg = await pg.require_backend().get_session(frozenset({b}), pg_ids[first])
    assert natural(detail_lite) == natural(detail_pg)
