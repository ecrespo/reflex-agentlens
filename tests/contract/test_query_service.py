"""``QueryService`` on both backends (plan tasks P4-02.1, P4-02.2, P4-02.3; API §6.1).

REQ-100/101 (pagination, URL filters), REQ-103 (trace tree, not found), REQ-104 (messages),
REQ-105 (sessions), REQ-151 (span drawer), REQ-191.c (a viewer limited to one Project never sees
another's data).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, get_args

import pytest

from reflex_agentlens.core.errors import Forbidden, NotFound, ValidationError
from reflex_agentlens.query import dto
from reflex_agentlens.query.filters import RunFilter, parse_runs_query
from reflex_agentlens.query.service import QueryService
from reflex_agentlens.store.records import NormalizedBatch, NormalizedSpan
from tests.contract.conftest import ContractDB
from tests.contract.dataset import dataset

T = TypeVar("T")
RUNS = dataset(5, 500)
OTHER = dataset(6, 200)
KEY = b"q" * 32
ADMIN = dto.Viewer("admin", "Admin", is_admin=True)


def _with_messages(runs: list[list[NormalizedSpan]]) -> list[list[NormalizedSpan]]:
    """Give the first LLM span of the first Run a stored conversation."""
    out = [list(run) for run in runs]
    for i, sp in enumerate(out[0]):
        if sp.llm_call is not None:
            attrs = dict(sp.attributes) | {
                "gen_ai.input.messages": [
                    {"role": "user", "parts": [{"type": "text", "content": "refund please"}]}
                ],
                "gen_ai.output.messages": [
                    {"role": "assistant", "parts": [{"type": "text", "content": "done"}]}
                ],
            }
            out[0][i] = dataclasses.replace(sp, attributes=attrs)
            break
    return out


async def load(db: ContractDB, slug: str, runs: list[list[NormalizedSpan]]) -> str:
    project = await db.make_project(slug)
    spans = tuple(sp for run in runs for sp in run)
    await db.require_backend().write_batch(NormalizedBatch(project, spans))
    return project


def service(db: ContractDB) -> QueryService:
    return QueryService(db.require_backend(), KEY, clock_ns=db.clock)


async def walk(fetch: Callable[[str | None], Awaitable[dto.Page[T]]]) -> list[T]:
    out: list[T] = []
    cursor: str | None = None
    while True:
        page = await fetch(cursor)
        out += page.items
        if page.next_cursor is None:
            return out
        cursor = page.next_cursor


@pytest.mark.parametrize("sort", get_args(dto.RunSort))
async def test_pagination_walks_every_run_once_req_100a(db: ContractDB, sort: Any) -> None:
    await load(db, "a", RUNS)
    q = service(db)
    items = await walk(lambda c: q.list_runs(ADMIN, RunFilter(), sort, c, limit=37))
    keys = [(x.project_id, x.trace_id) for x in items]
    assert len(keys) == len(set(keys)) == len(RUNS)


async def test_url_filters_reach_storage_req_101(db: ContractDB) -> None:
    await load(db, "a", RUNS)
    q = service(db)
    everything = await walk(lambda c: q.list_runs(ADMIN, RunFilter(), cursor=c, limit=200))
    for status in {x.status for x in everything}:
        f = parse_runs_query(f"status={status}").filter

        def fetch(c: str | None, f: RunFilter = f) -> Awaitable[dto.Page[dto.RunSummary]]:
            return q.list_runs(ADMIN, f, cursor=c, limit=200)

        hits_status = await walk(fetch)
        assert len(hits_status) == sum(x.status == status for x in everything)
    by_model = parse_runs_query("model=gpt-4o&project=a&tok=1..").filter
    hits = await walk(lambda c: q.list_runs(ADMIN, by_model, cursor=c))
    assert hits
    assert all("gpt-4o" in x.models and x.total_tokens >= 1 for x in hits)
    source = hits[0].source
    assert source is not None
    by_source = RunFilter(sources=[source.label])
    same_source = await walk(lambda c: q.list_runs(ADMIN, by_source, cursor=c))
    assert {x.source.label for x in same_source if x.source} == {source.label}


async def test_cursor_of_another_sort_is_refused(db: ContractDB) -> None:
    await load(db, "a", RUNS)
    q = service(db)
    first = await q.list_runs(ADMIN, RunFilter(), "start_desc", limit=5)
    assert first.next_cursor is not None
    with pytest.raises(ValidationError) as exc:
        await q.list_runs(ADMIN, RunFilter(), "cost_desc", first.next_cursor, limit=5)
    assert exc.value.field == "cursor"


async def test_viewer_limited_to_one_project_req_191c(db: ContractDB) -> None:
    a = await load(db, "a", RUNS)
    b = await load(db, "b", OTHER)
    q = service(db)
    viewer = dto.Viewer("v", "V", is_admin=False, allowed_project_ids=frozenset({a}))
    runs = await walk(lambda c: q.list_runs(viewer, RunFilter(), cursor=c, limit=200))
    assert {x.project_id for x in runs} == {a}
    with pytest.raises(Forbidden):
        await q.list_runs(viewer, RunFilter(project_ids=["b"]))
    assert {p.id for p in await q.list_projects(viewer)} == {a}
    assert {s.project_id for s in await q.list_sources(viewer)} == {a}
    with pytest.raises(Forbidden):
        await q.list_sources(viewer, [b])
    other = (await q.list_runs(ADMIN, RunFilter(project_ids=[b]), limit=1)).items[0]
    assert await q.resolve_trace(viewer, other.trace_id) == []
    with pytest.raises(Forbidden):
        await q.get_trace(viewer, other.ref)
    with pytest.raises(Forbidden):
        await q.list_llm_calls(viewer, other.ref)
    with pytest.raises(Forbidden):
        await q.get_project(viewer, b)
    sessions = await walk(lambda c: q.list_sessions(viewer, dto.SessionFilter(), cursor=c))
    assert {s.project_id for s in sessions} == {a}
    b_sessions = await q.list_sessions(ADMIN, dto.SessionFilter(project_ids=[b]))
    with pytest.raises(NotFound):
        await q.get_session(viewer, b_sessions.items[0].session_id)
    scope = dto.AggScope(time_from="2026-01-01T00:00:00Z")
    models_a = await q.facets(viewer, "model", scope)
    models_all = await q.facets(ADMIN, "model", scope)
    assert set(models_a) <= set(models_all)


async def test_trace_detail_req_103(db: ContractDB) -> None:
    await load(db, "a", RUNS)
    q = service(db)
    run = (await q.list_runs(ADMIN, RunFilter(), "tokens_desc", limit=1)).items[0]
    assert await q.resolve_trace(ADMIN, run.trace_id) == [run.ref]
    assert await q.resolve_trace(ADMIN, run.trace_id.upper(), "a") == [run.ref]
    detail = await q.get_trace(ADMIN, run.ref)
    assert detail.run == run
    assert len(detail.spans) == run.span_count
    assert detail.next_offset is None
    assert detail.totals.total_tokens == run.total_tokens
    assert len(detail.llm_calls) == run.llm_calls
    roots = [n for n in detail.spans if n.depth == 0]
    assert roots[0].subtree_tokens == run.total_tokens
    paged = await q.get_trace(ADMIN, run.ref, offset=0, limit=3)
    assert paged.spans == detail.spans[:3]
    assert paged.next_offset == 3
    missing = dto.RunRef(project_id=run.project_id, trace_id="ab" * 16)
    with pytest.raises(NotFound):
        await q.get_trace(ADMIN, missing)
    with pytest.raises(ValidationError):
        await q.get_trace(ADMIN, run.ref, limit=5000)
    with pytest.raises(ValidationError):
        await q.resolve_trace(ADMIN, "not-hex")


async def test_llm_messages_and_span_drawer_req_104_151(db: ContractDB) -> None:
    runs = _with_messages(RUNS)
    project = await load(db, "a", runs)
    q = service(db)
    ref = dto.RunRef(project_id=project, trace_id=runs[0][0].trace_id.hex())
    calls = await q.list_llm_calls(ADMIN, ref)
    with_messages = [c for c in calls if c.messages]
    assert len(with_messages) == 1
    assert [m.parts[0].content for m in with_messages[0].messages] == ["refund please", "done"]
    detail = await q.get_span_detail(ADMIN, ref, with_messages[0].span_id)
    assert detail.llm is not None
    assert detail.llm.messages == with_messages[0].messages
    assert detail.span.kind == "llm"
    assert detail.raw.span_id == with_messages[0].span_id
    tool_span = next(sp for sp in runs[0] if sp.tool_call is not None)
    tool_detail = await q.get_span_detail(ADMIN, ref, tool_span.span_id.hex())
    assert tool_detail.tool is not None
    assert tool_detail.span.kind == "tool"
    raw = await q.get_span_raw(ADMIN, ref, tool_span.span_id.hex())
    assert raw == tool_detail.raw
    with pytest.raises(NotFound):
        await q.get_span_detail(ADMIN, ref, "cd" * 8)
    with pytest.raises(NotFound):
        await q.get_span_raw(ADMIN, ref, "cd" * 8)


@pytest.mark.parametrize("sort", get_args(dto.SessionSort))
async def test_sessions_req_105(db: ContractDB, sort: Any) -> None:
    await load(db, "a", RUNS)
    q = service(db)
    sessions = await walk(lambda c: q.list_sessions(ADMIN, dto.SessionFilter(), sort, c, limit=7))
    ids = [s.session_id for s in sessions]
    assert len(ids) == len(set(ids))
    assert ids
    detail = await q.get_session(ADMIN, ids[0])
    assert len(detail.runs) == detail.session.run_count
    starts = [x.run.start_ns for x in detail.runs]
    assert starts == sorted(starts)
    cumulative = [x.cumulative_tokens for x in detail.runs]
    assert cumulative == sorted(cumulative)
    assert cumulative[-1] == detail.session.total_tokens
    with pytest.raises(NotFound):
        await q.get_session(ADMIN, "01JB0000000000000000000000")


async def test_projects_and_facets(db: ContractDB) -> None:
    a = await load(db, "a", RUNS)
    q = service(db)
    project = await q.get_project(ADMIN, a)
    assert (project.slug, project.retention_days) == ("a", 30)
    with pytest.raises(NotFound):
        await q.get_project(ADMIN, "missing")
    scope = dto.AggScope(time_from="2026-01-01T00:00:00Z")
    statuses = await q.facets(ADMIN, "status", scope)
    assert set(statuses) <= {"in_progress", "ok", "error", "incomplete"}
    assert statuses
    with pytest.raises(ValidationError):
        await q.facets(ADMIN, "model", scope, limit=0)
