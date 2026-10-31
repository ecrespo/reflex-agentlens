"""Run and trace read contract tests (plan task P2-07.1; Data Model Q1-Q5b).

REQ-100 (list, sort, keyset), REQ-101 (every filter, validation), REQ-103 (trace detail),
REQ-104 (LLM call access), API §5 (scope isolation). Each filter is checked against an oracle
computed in Python from the seeded dataset or from the unfiltered list, on both backends.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.search import search_tokens
from reflex_agentlens.store.records import (
    AttrPredicate,
    NormalizedBatch,
    NormalizedSpan,
    Range,
    RunKey,
    RunQuery,
    RunSort,
    RunSummary,
    SourceKey,
    TimeRange,
)
from tests.contract.conftest import ContractDB
from tests.contract.dataset import BASE_NS, NS, SOURCES, dataset

RUNS = dataset(7, 1500)
DAY = 86_400 * NS


async def load(db: ContractDB, runs: list[list[NormalizedSpan]] = RUNS) -> str:
    project = await db.make_project()
    spans = tuple(sp for run in runs for sp in run)
    await db.require_backend().write_batch(NormalizedBatch(project, spans))
    return project


async def all_runs(db: ContractDB, q: RunQuery, page: int = 200) -> list[RunSummary]:
    backend = db.require_backend()
    out: list[RunSummary] = []
    q = dataclasses.replace(q, limit=page)
    while True:
        res = await backend.list_runs(q)
        out += res.items
        if res.next_cursor is None:
            return out
        q = dataclasses.replace(q, cursor=res.next_cursor)


def keys(runs: list[RunSummary]) -> set[bytes]:
    return {r.trace_id for r in runs}


def by_trace() -> dict[bytes, list[NormalizedSpan]]:
    return {run[0].trace_id: run for run in RUNS}


# ── REQ-100: list, sort and keyset pagination ────────────────────────────────────────────────


SORT_KEYS: dict[str, Callable[[RunSummary], int | None]] = {
    "start": lambda r: r.start_ns,
    "duration": lambda r: r.duration_ns,
    "tokens": lambda r: r.input_tokens + r.output_tokens,
    "cost": lambda r: r.cost_nusd,
}


@pytest.mark.parametrize(
    "sort",
    [f"{k}_{d}" for k in SORT_KEYS for d in ("desc", "asc")],
)
async def test_sorted_keyset_pages_cover_every_run_once(db: ContractDB, sort: RunSort) -> None:
    """REQ-100.a: every sort; pages of 7 concatenate to the full ordered list, no dup or gap."""
    project = await load(db)
    scope = frozenset({project})
    paged = await all_runs(db, RunQuery(scope, sort=sort), page=7)
    assert len(paged) == len(RUNS)
    assert keys(paged) == set(by_trace())
    field, direction = sort.rsplit("_", 1)
    values = [SORT_KEYS[field](r) for r in paged]
    present = [v for v in values if v is not None]
    assert present == sorted(present, reverse=direction == "desc")
    first_none = next((i for i, v in enumerate(values) if v is None), len(values))
    assert all(v is None for v in values[first_none:]), "NULL sort keys come last"
    whole = (await db.require_backend().list_runs(RunQuery(scope, sort=sort, limit=200))).items
    assert [r.trace_id for r in whole] == [r.trace_id for r in paged[: len(whole)]]


async def test_default_page_and_summary_fields(db: ContractDB) -> None:
    """REQ-100.a/b: default 50 newest first, with the per-Run fields filled."""
    project = await load(db)
    page = await db.require_backend().list_runs(RunQuery(frozenset({project})))
    assert len(page.items) == 50
    assert page.next_cursor is not None
    newest = max(run[0].start_ns for run in RUNS)
    top = page.items[0]
    assert top.start_ns == newest
    root = by_trace()[top.trace_id][0]
    assert (top.project_id, top.root_name, top.agent_name) == (project, root.name, root.agent_name)
    assert top.source == root.source
    assert top.span_count == len(by_trace()[top.trace_id])
    assert top.session_id is not None
    assert top.is_genai
    assert top.models
    assert top.key == RunKey(project, top.trace_id)


async def test_keyset_is_stable_under_inserts(db: ContractDB) -> None:
    """REQ-100.a: Runs inserted between page fetches never make old Runs repeat or vanish."""
    backend = db.require_backend()
    first_half, second_half = RUNS[: len(RUNS) // 2], RUNS[len(RUNS) // 2 :]
    project = await load(db, first_half)
    scope = frozenset({project})
    page = await backend.list_runs(RunQuery(scope, limit=10))
    await backend.write_batch(
        NormalizedBatch(project, tuple(sp for run in second_half for sp in run))
    )
    rest = await all_runs(db, RunQuery(scope, cursor=page.next_cursor), page=10)
    last = page.items[-1]
    expected = sorted(
        ((run[0].start_ns, run[0].trace_id) for run in RUNS
         if (run[0].start_ns, run[0].trace_id) < (last.start_ns, last.trace_id)),
        reverse=True,
    )  # fmt: skip
    assert [(r.start_ns, r.trace_id) for r in rest] == expected
    old = {run[0].trace_id for run in first_half}
    assert old <= keys(page.items) | keys(rest)
    assert not keys(page.items) & keys(rest)


async def test_limit_validation(db: ContractDB) -> None:
    project = await load(db, RUNS[:3])
    for bad in (0, 201):
        with pytest.raises(ValidationError):
            await db.require_backend().list_runs(RunQuery(frozenset({project}), limit=bad))


# ── API §5: scope ────────────────────────────────────────────────────────────────────────────


async def test_scope_isolation(db: ContractDB) -> None:
    """Two Projects with identical traces: each scope sees only its own; empty scope → nothing."""
    backend = db.require_backend()
    a, b = await load(db, RUNS[:20]), await load(db, RUNS[:20])
    only_a = await all_runs(db, RunQuery(frozenset({a})))
    both = await all_runs(db, RunQuery(frozenset({a, b})))
    assert {r.project_id for r in only_a} == {a}
    assert len(only_a) == 20
    assert len(both) == 40
    assert (await backend.list_runs(RunQuery(frozenset()))).items == []
    assert (await backend.list_runs(RunQuery(frozenset({"01NOTAPROJECT"})))).items == []
    trace = RUNS[0][0].trace_id
    assert await backend.find_runs_by_trace_id(frozenset({a, b}), trace) == sorted(
        [RunKey(a, trace), RunKey(b, trace)]
    )
    assert await backend.find_runs_by_trace_id(frozenset({b}), trace) == [RunKey(b, trace)]
    assert await backend.find_runs_by_trace_id(frozenset(), trace) == []
    assert await backend.get_trace(RunKey("01NOTAPROJECT", trace)) is None


# ── REQ-101: filters against an oracle ──────────────────────────────────────────────────────


def _tools(trace: bytes) -> set[str]:
    return {sp.tool_call.tool_name for sp in by_trace()[trace] if sp.tool_call}


def _matches_text(trace: bytes, query: str) -> bool:
    want = set(search_tokens(query))
    return any(want <= set(search_tokens(sp.search_doc or "")) for sp in by_trace()[trace])


MID = BASE_NS + 15 * DAY

FILTERS: dict[str, tuple[dict[str, Any], Callable[[RunSummary], bool]]] = {
    "source": (
        {"sources": (SOURCES[0], SOURCES[2])},
        lambda r: r.source in (SOURCES[0], SOURCES[2]),
    ),
    "time_range": (
        {"time_range": TimeRange(MID, MID + 5 * DAY)},
        lambda r: MID <= r.start_ns < MID + 5 * DAY,
    ),
    "status": ({"statuses": ("error",)}, lambda r: r.status == "error"),
    "is_genai": ({"is_genai": True}, lambda r: r.is_genai),
    "model": ({"models": ("gpt-4o",)}, lambda r: "gpt-4o" in r.models),
    "tool": ({"tools": ("RefundTool", "kb-lookup")},
             lambda r: bool(_tools(r.trace_id) & {"RefundTool", "kb-lookup"})),
    "duration": (
        {"duration_ns": Range(5 * NS, 20 * NS)},
        lambda r: r.duration_ns is not None and 5 * NS <= r.duration_ns <= 20 * NS,
    ),
    "tokens": (
        {"tokens": Range(min=30_000)},
        lambda r: r.input_tokens + r.output_tokens >= 30_000,
    ),
    "cost": (
        {"cost_nusd": Range(max=50_000_000)},
        lambda r: r.unpriced_calls == 0 and r.cost_nusd is not None and r.cost_nusd <= 50_000_000,
    ),
    "cost_unpriced": (
        {"cost_nusd": Range(max=50_000_000), "include_unpriced_in_cost_range": True},
        lambda r: r.unpriced_calls > 0
        or (r.cost_nusd is not None and r.cost_nusd <= 50_000_000),
    ),
    "attr_eq_root": (
        {"attributes": (AttrPredicate("gen_ai.agent.name", value="refunds"),)},
        lambda r: r.agent_name == "refunds",
    ),
    "attr_exists_resource": (
        {"attributes": (AttrPredicate("service.namespace", scope="resource", op="exists"),)},
        lambda r: r.source is not None and r.source.service_namespace != "",
    ),
    "attr_missing": (
        {"attributes": (AttrPredicate("no.such.key", op="exists"),)},
        lambda r: False,
    ),
    "text": ({"text": "Refund POLICY"}, lambda r: _matches_text(r.trace_id, "refund policy")),
    "text_accents": ({"text": "cafe"}, lambda r: _matches_text(r.trace_id, "café")),
    "text_error": ({"text": "max turns"}, lambda r: _matches_text(r.trace_id, "max turns")),
    "combined": (
        {"statuses": ("ok", "error"), "models": ("gpt-4o-mini",), "sort": "cost_desc"},
        lambda r: r.status in ("ok", "error") and "gpt-4o-mini" in r.models,
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", list(FILTERS))
async def test_filter_matches_oracle(db: ContractDB, name: str) -> None:
    """REQ-101.a-h: each filter returns exactly the Runs its oracle selects."""
    project = await load(db)
    scope = frozenset({project})
    everything = await all_runs(db, RunQuery(scope))
    kwargs, oracle = FILTERS[name]
    got = await all_runs(db, RunQuery(scope, **kwargs), page=23)
    expected = {r.trace_id for r in everything if oracle(r)}
    assert keys(got) == expected
    assert len(got) == len(expected)
    if name not in ("attr_missing",):
        assert expected, f"oracle for {name} selects nothing; the dataset no longer exercises it"


async def test_session_filter(db: ContractDB) -> None:
    """REQ-101.h."""
    project = await load(db)
    scope = frozenset({project})
    everything = await all_runs(db, RunQuery(scope))
    sid = next(r.session_id for r in everything if r.session_id)
    got = await all_runs(db, RunQuery(scope, session_id=sid))
    assert keys(got) == {r.trace_id for r in everything if r.session_id == sid}
    assert await all_runs(db, RunQuery(scope, session_id="01NOSUCHSESSION")) == []


@pytest.mark.parametrize(
    "query",
    [
        RunQuery(frozenset({"x"}), duration_ns=Range(10, 1)),
        RunQuery(frozenset({"x"}), attributes=(AttrPredicate("bad key!", value=1),)),
        RunQuery(frozenset({"x"}), attributes=(AttrPredicate("k", value=None),)),
        RunQuery(frozenset({"x"}), attributes=(AttrPredicate("k'); --", op="exists"),)),
    ],
)
async def test_malformed_filters_are_validation_errors(db: ContractDB, query: RunQuery) -> None:
    """REQ-101.i: never an unfiltered result."""
    project = await load(db, RUNS[:2])
    scoped = dataclasses.replace(query, project_ids=frozenset({project}))
    with pytest.raises(ValidationError):
        await db.require_backend().list_runs(scoped)


async def test_text_without_terms_is_ignored_and_injection_is_inert(db: ContractDB) -> None:
    """REQ-325.b: punctuation-only text has no terms; FTS operators are just words."""
    project = await load(db, RUNS[:10])
    scope = frozenset({project})
    assert len(await all_runs(db, RunQuery(scope, text=" ,;! "))) == 10
    for hostile in ('" OR 1=1 --', "refund* NEAR(x)", "a & b | !c", "'; DROP TABLE runs; --"):
        got = await all_runs(db, RunQuery(scope, text=hostile))
        assert keys(got) == {
            t for t in by_trace() if t in keys(await all_runs(db, RunQuery(scope)))
            and _matches_text(t, hostile)
        }  # fmt: skip


# ── REQ-103 / REQ-104 / Q5b: trace detail ────────────────────────────────────────────────────


async def test_get_trace_returns_ordered_spans_with_derived_rows(db: ContractDB) -> None:
    """REQ-103.a: every span, ordered by (start, span_id), with its derived rows."""
    project = await load(db, RUNS[:5])
    backend = db.require_backend()
    run = RUNS[3]
    detail = await backend.get_trace(RunKey(project, run[0].trace_id))
    assert detail is not None
    assert detail.run.trace_id == run[0].trace_id
    assert detail.run.span_count == len(run)
    expected = sorted(run, key=lambda sp: (sp.start_ns, sp.span_id))
    assert [sp.span_id for sp in detail.spans] == [sp.span_id for sp in expected]
    for got, want in zip(detail.spans, expected, strict=True):
        assert (got.parent_span_id, got.name, got.category) == (
            want.parent_span_id, want.name, want.category,
        )  # fmt: skip
        assert (got.start_ns, got.end_ns, got.status_code, got.source) == (
            want.start_ns, want.end_ns, want.status_code, want.source,
        )  # fmt: skip
        assert got.llm_call == want.llm_call
        assert got.tool_call == want.tool_call
        assert got.approval == want.approval
        assert got.graph_node == want.graph_node


async def test_unknown_trace_is_none(db: ContractDB) -> None:
    """REQ-103.b."""
    project = await load(db, RUNS[:1])
    backend = db.require_backend()
    missing = RunKey(project, b"\x00" * 16)
    assert await backend.get_trace(missing) is None
    assert await backend.get_span_raw(missing, b"\x00" * 8) is None
    assert await backend.list_llm_calls(missing) == []
    known = RunKey(project, RUNS[0][0].trace_id)
    assert await backend.get_span_raw(known, b"\x00" * 8) is None


async def test_get_span_raw(db: ContractDB) -> None:
    """Q5b: raw attributes, resource, events and links round-trip."""
    project = await load(db, RUNS[:2])
    sp = RUNS[1][2]
    raw = await db.require_backend().get_span_raw(RunKey(project, sp.trace_id), sp.span_id)
    assert raw is not None
    assert raw.span_id == sp.span_id
    assert raw.attributes == sp.attributes
    assert raw.resource == sp.resource.attributes
    assert (raw.events, raw.links, raw.dropped_keys) == (sp.events, sp.links, sp.dropped_keys)


async def test_list_llm_calls(db: ContractDB) -> None:
    """REQ-104.a: every LLM call of the Run, in start order, with raw attributes."""
    project = await load(db, RUNS[:4])
    run = RUNS[2]
    calls = await db.require_backend().list_llm_calls(RunKey(project, run[0].trace_id))
    want = sorted((sp for sp in run if sp.llm_call), key=lambda sp: (sp.start_ns, sp.span_id))
    assert [c.span_id for c in calls] == [sp.span_id for sp in want]
    assert [c.call for c in calls] == [sp.llm_call for sp in want]
    assert [c.attributes for c in calls] == [sp.attributes for sp in want]


# ── sources and facets ───────────────────────────────────────────────────────────────────────


async def test_list_sources_and_distinct_values(db: ContractDB) -> None:
    project = await load(db)
    backend = db.require_backend()
    scope = frozenset({project})
    sources = await backend.list_sources(scope)
    assert {v.source for v in sources} == {sp.source for run in RUNS for sp in run}
    assert all(v.project_id == project and v.first_seen_ns <= v.last_seen_ns for v in sources)
    assert await backend.list_sources(frozenset()) == []

    window = TimeRange(BASE_NS, BASE_NS + 60 * DAY)
    spans = [sp for run in RUNS for sp in run]
    models = sorted({sp.llm_call.model for sp in spans if sp.llm_call})
    tools = sorted({sp.tool_call.tool_name for sp in spans if sp.tool_call})
    assert await backend.distinct_values(scope, "model", window) == models
    assert await backend.distinct_values(scope, "tool", window) == tools
    assert await backend.distinct_values(scope, "model", window, 2) == models[:2]
    agents = sorted({run[0].agent_name for run in RUNS if run[0].agent_name})
    assert await backend.distinct_values(scope, "agent", window) == agents
    statuses = await backend.distinct_values(scope, "status", window)
    assert set(statuses) <= {"ok", "error", "incomplete", "in_progress"}
    assert statuses
    labels = await backend.distinct_values(scope, "source", window)
    assert labels == sorted(
        f"{k.service_namespace}/{k.service_name}" if k.service_namespace else k.service_name
        for k in {sp.source for sp in spans}
    )
    empty = TimeRange(BASE_NS - 10 * DAY, BASE_NS - 9 * DAY)
    assert await backend.distinct_values(scope, "model", empty) == []
    assert await backend.distinct_values(frozenset(), "model", window) == []
    assert SourceKey("support-agent", "shop") in {v.source for v in sources}
