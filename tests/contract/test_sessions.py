"""Session read contract tests (plan task P2-07.2; Data Model Q9; REQ-105)."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Callable

import pytest

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.store.records import (
    NormalizedBatch,
    RunQuery,
    RunSummary,
    SessionKey,
    SessionQuery,
    SessionSort,
    SessionSummary,
    TimeRange,
)
from tests.contract.conftest import ContractDB
from tests.contract.dataset import BASE_NS, NS, SOURCES, dataset
from tests.contract.test_reads import all_runs, load

RUNS = dataset(11, 1500)
DAY = 86_400 * NS


async def all_sessions(db: ContractDB, q: SessionQuery, page: int = 200) -> list[SessionSummary]:
    backend = db.require_backend()
    out: list[SessionSummary] = []
    q = dataclasses.replace(q, limit=page)
    while True:
        res = await backend.list_sessions(q)
        out += res.items
        if res.next_cursor is None:
            return out
        q = dataclasses.replace(q, cursor=res.next_cursor)


async def runs_by_session(db: ContractDB, project: str) -> dict[str, list[RunSummary]]:
    out: dict[str, list[RunSummary]] = defaultdict(list)
    for r in await all_runs(db, RunQuery(frozenset({project}))):
        if r.session_id:
            out[r.session_id].append(r)
    return out


SORTS: dict[str, Callable[[SessionSummary], int]] = {
    "last": lambda x: x.last_ns,
    "runs": lambda x: x.run_count,
    "tokens": lambda x: x.input_tokens + x.output_tokens,
    "cost": lambda x: x.cost_nusd,
}


@pytest.mark.parametrize("sort", [f"{k}_{d}" for k in SORTS for d in ("desc", "asc")])
async def test_sessions_sorted_and_paged(db: ContractDB, sort: SessionSort) -> None:
    """REQ-105.a: every sort key, keyset pages of 6 cover each Session once in order."""
    project = await load(db, RUNS)
    expected = await runs_by_session(db, project)
    got = await all_sessions(db, SessionQuery(frozenset({project}), sort=sort), page=6)
    assert [x.session_id for x in got] != []
    assert sorted(x.session_id for x in got) == sorted(expected)
    assert len({x.session_id for x in got}) == len(got)
    field, direction = sort.rsplit("_", 1)
    values = [SORTS[field](x) for x in got]
    assert values == sorted(values, reverse=direction == "desc")
    for x in got:
        runs = expected[x.session_id]
        assert x.run_count == len(runs)
        assert x.input_tokens == sum(r.input_tokens for r in runs)
        assert x.output_tokens == sum(r.output_tokens for r in runs)
        assert x.cost_nusd == sum(r.cost_nusd or 0 for r in runs)
        assert x.includes_unpriced == any(r.unpriced_calls > 0 for r in runs)
        assert x.first_ns == min(r.start_ns for r in runs)
        assert x.project_id == project


async def test_session_filters(db: ContractDB) -> None:
    """REQ-105.a: Source and time-range (overlap) filters; scope isolation."""
    project = await load(db, RUNS)
    other = await load(db, RUNS[:10])
    scope = frozenset({project})
    everything = await all_sessions(db, SessionQuery(scope))
    expected = await runs_by_session(db, project)

    by_source = await all_sessions(db, SessionQuery(scope, sources=(SOURCES[1],)))
    assert {x.session_id for x in by_source} == {
        sid for sid, runs in expected.items() if any(r.source == SOURCES[1] for r in runs)
    }
    window = TimeRange(BASE_NS + 10 * DAY, BASE_NS + 12 * DAY)
    in_window = await all_sessions(db, SessionQuery(scope, time_range=window))
    assert {x.session_id for x in in_window} == {
        x.session_id
        for x in everything
        if x.last_ns >= window.start_ns and x.first_ns < window.end_ns
    }
    assert in_window
    assert not {x.project_id for x in everything} - {project}
    both = await all_sessions(db, SessionQuery(frozenset({project, other})))
    assert len(both) > len(everything)
    assert (await db.require_backend().list_sessions(SessionQuery(frozenset()))).items == []
    with pytest.raises(ValidationError):
        await db.require_backend().list_sessions(SessionQuery(scope, limit=0))


async def test_get_session_runs_with_running_totals(db: ContractDB) -> None:
    """REQ-105.b: Runs in chronological order with per-Run and cumulative tokens and cost."""
    project = await load(db, RUNS)
    expected = await runs_by_session(db, project)
    sid = max(expected, key=lambda k: len(expected[k]))
    assert len(expected[sid]) > 1
    detail = await db.require_backend().get_session(frozenset({project}), sid)
    assert detail is not None
    assert detail.session.session_id == sid
    runs = sorted(expected[sid], key=lambda r: (r.start_ns, r.trace_id))
    assert [x.run.trace_id for x in detail.runs] == [r.trace_id for r in runs]
    tokens = cost = 0
    unpriced = False
    for x, r in zip(detail.runs, runs, strict=True):
        tokens += r.input_tokens + r.output_tokens
        cost += r.cost_nusd or 0
        unpriced = unpriced or r.unpriced_calls > 0
        assert (x.cumulative_tokens, x.cumulative_cost_nusd) == (tokens, cost)
        assert x.cumulative_includes_unpriced == unpriced
        assert x.run == r
    assert await db.require_backend().get_session(frozenset({"01OTHER"}), sid) is None
    assert await db.require_backend().get_session(frozenset(), sid) is None
    assert await db.require_backend().get_session(frozenset({project}), "01NOPE") is None


async def test_session_without_runs_is_hidden(db: ContractDB) -> None:
    """Data Model §2.8: a Session no Run belongs to is neither listed nor returned."""
    project = await load(db, RUNS[:3])
    run = RUNS[0]
    stray = dataclasses.replace(
        run[1], span_id=b"\xfe" * 8, session=SessionKey("conversation-with-no-run")
    )
    await db.require_backend().write_batch(NormalizedBatch(project, (stray,)))
    count = await db.scalar(
        "SELECT count(*) FROM sessions WHERE external_id = 'conversation-with-no-run'"
    )
    assert count == 1  # created on first sight (REQ-036.a) ...
    listed = await all_sessions(db, SessionQuery(frozenset({project})))
    assert "conversation-with-no-run" not in {x.external_id for x in listed}  # ... but hidden
    sid = await db.scalar("SELECT id FROM sessions WHERE external_id = 'conversation-with-no-run'")
    assert isinstance(sid, str)
    assert await db.require_backend().get_session(frozenset({project}), sid) is None
