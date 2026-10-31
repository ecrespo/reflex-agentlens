"""Derived-view maintenance contract tests (plan task P2-08.3).

REQ-032.b (finalizer), REQ-042.a/b (rebuild, idempotent, safe during ingestion), REQ-062.a
(cost recompute), DD-004 (finalizer skips Runs another writer holds).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from sqlalchemy import text

from reflex_agentlens.core.pricing import llm_cost, match_entry
from reflex_agentlens.store.records import (
    NormalizedBatch,
    NormalizedSpan,
    PricingEntry,
    PricingVersion,
    PricingVersionMeta,
    RebuildScope,
    RunKey,
    RunQuery,
    SessionKey,
    TimeRange,
)
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.dataset import BASE_NS, NS, dataset
from tests.contract.snapshot import snapshot
from tests.contract.test_reads import all_runs, load
from tests.contract.test_write_batch import WINDOW, span, write

PRICING = PricingVersion(PricingVersionMeta(7, 0, "editor", None, 0, None), ())


async def snap(db: ContractDB, project: str) -> dict[str, Any]:
    async with db.engine.connect() as conn:
        out = await conn.run_sync(snapshot, project)
    out.pop("session_ids")  # Sessions orphaned by a re-derivation stay until purge (§2.8)
    return out


async def run_status(db: ContractDB, project: str, trace: int) -> object:
    return await db.scalar(
        "SELECT status FROM runs WHERE trace_id = :t AND project_pk = "
        "(SELECT pk FROM projects WHERE id = :p)",
        t=trace.to_bytes(16, "big"),
        p=project,
    )


# ── finalizer (REQ-032.b) ────────────────────────────────────────────────────────────────────


async def test_finalizer_marks_stale_runs_incomplete(db: ContractDB) -> None:
    backend = db.require_backend()
    project = await db.make_project()
    # trace 1: only a child, last span 60 s ago → in_progress; trace 2: child 10 min ago
    await write(db, project, span(1, 2, 1, 70, 60), span(2, 2, 1, 700, 600))
    assert await run_status(db, project, 1) == "in_progress"
    assert await run_status(db, project, 2) == "incomplete"  # already past the window at write

    await write(db, project, span(3, 2, 1, 50, 40))
    db.clock.now_ns = NOW_NS + WINDOW  # trace 1 (60 s + 5 min) is now stale, trace 3 too
    keys = await backend.finalize_stale_runs(db.clock.now_ns - WINDOW, 500)
    assert keys == sorted(RunKey(project, t.to_bytes(16, "big")) for t in (1, 3))
    assert await run_status(db, project, 1) == "incomplete"
    assert await backend.finalize_stale_runs(db.clock.now_ns - WINDOW, 500) == []

    db.clock.now_ns = NOW_NS
    await write(db, project, span(4, 2, 1, 10, 5), span(5, 2, 1, 30, 20))
    db.clock.now_ns = NOW_NS + 10 * WINDOW
    assert len(await backend.finalize_stale_runs(db.clock.now_ns - WINDOW, 1)) == 1  # limit
    assert len(await backend.finalize_stale_runs(db.clock.now_ns - WINDOW, 10)) == 1


async def test_late_root_reopens_nothing_and_finalizer_respects_threshold(db: ContractDB) -> None:
    """A Run that got its root is ``ok``/``error`` and never touched by the finalizer."""
    backend = db.require_backend()
    project = await db.make_project()
    await write(db, project, span(1, 2, 1, 70, 60))
    await write(db, project, span(1, 1, None, 80, 50))
    db.clock.now_ns += 10 * WINDOW
    assert await backend.finalize_stale_runs(db.clock.now_ns, 500) == []
    assert await run_status(db, project, 1) == "ok"


@pytest.mark.pg
async def test_finalizer_skips_runs_locked_by_another_writer(db: ContractDB) -> None:
    """DD-004 / Q12: ``SKIP LOCKED``; the locked Run is finalized on a later pass."""
    if db.name != "pg":
        pytest.skip("row locks are a PostgreSQL concern")
    backend = db.require_backend()
    project = await db.make_project()
    await write(db, project, span(1, 2, 1, 70, 60), span(2, 2, 1, 70, 60))
    db.clock.now_ns += 10 * WINDOW
    async with db.engine.connect() as other:
        await other.begin()
        await other.execute(
            text("SELECT pk FROM runs WHERE trace_id = :t FOR UPDATE"),
            {"t": (1).to_bytes(16, "big")},
        )
        keys = await asyncio.wait_for(backend.finalize_stale_runs(db.clock.now_ns, 500), 10)
        assert keys == [RunKey(project, (2).to_bytes(16, "big"))]
        await other.rollback()
    keys = await backend.finalize_stale_runs(db.clock.now_ns, 500)
    assert keys == [RunKey(project, (1).to_bytes(16, "big"))]


# ── rebuild (REQ-042) ────────────────────────────────────────────────────────────────────────


def identity(sp: NormalizedSpan, pricing: PricingVersion) -> NormalizedSpan:
    return sp


def remap(sp: NormalizedSpan, pricing: PricingVersion = PRICING) -> NormalizedSpan:
    """A "new mapping": agents renamed, tool calls dropped, Sessions re-keyed, docs rebuilt."""

    def v2(value: str, sep: str) -> str:
        return value if value.startswith(f"v2{sep}") else f"v2{sep}{value}"

    return dataclasses.replace(
        sp,
        agent_name=None if sp.agent_name is None else v2(sp.agent_name, "-"),
        tool_call=None,
        category="generic" if sp.category == "tool" else sp.category,
        session=None if sp.session is None else SessionKey(v2(sp.session.external_id, ":")),
        mapping_version=2,
        search_doc=f"rebuilt {sp.name}",  # a pure function of the raw span, hence idempotent
    )


async def test_rebuild_is_idempotent_and_equals_fresh_ingest(db: ContractDB) -> None:
    """REQ-042.a: the result equals ingesting the re-derived spans; a second run changes nothing."""
    backend = db.require_backend()
    runs = dataset(13, 600)
    project = await load(db, runs)
    before = await snap(db, project)
    report = await backend.rebuild(RebuildScope(), identity, PRICING, batch_runs=7)
    assert (report.runs_scanned, report.runs_changed) == (len(runs), 0)
    assert report.spans_rederived == sum(len(run) for run in runs)
    assert await snap(db, project) == before

    report = await backend.rebuild(RebuildScope(frozenset({project})), remap, PRICING, batch_runs=7)
    assert report.runs_changed == len(runs)  # every Run had tool calls or an agent
    reference = await db.make_project()
    await backend.write_batch(
        NormalizedBatch(reference, tuple(remap(sp) for run in runs for sp in run))
    )
    got, want = await snap(db, project), await snap(db, reference)
    for key in ("runs", "sessions", "llm_calls", "tool_calls", "approvals", "graph_nodes"):
        assert _drop_ids(got[key]) == _drop_ids(want[key]), key
    again = await backend.rebuild(RebuildScope(frozenset({project})), remap, PRICING)
    assert again.runs_changed == 0

    hits = await all_runs(db, RunQuery(frozenset({project}), text="rebuilt"))
    assert len(hits) == len(runs)  # search documents were replaced
    assert await all_runs(db, RunQuery(frozenset({project}), text="refund")) == []
    # every stored span (both Projects) now carries the new mapping version
    assert await db.scalar("SELECT value FROM app_meta WHERE key = 'mapping_version_min'") == "2"


def _drop_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k not in ("id",)} for row in rows]


async def test_rebuild_scope(db: ContractDB) -> None:
    """``--project`` and ``--since`` limit the Runs visited."""
    backend = db.require_backend()
    runs = dataset(14, 300)
    a, b = await load(db, runs), await load(db, runs)
    report = await backend.rebuild(RebuildScope(frozenset({a})), remap, PRICING)
    assert report.runs_scanned == len(runs)
    untouched = await all_runs(db, RunQuery(frozenset({b})))
    assert all(not (r.agent_name or "").startswith("v2-") for r in untouched)
    since = sorted(max(sp.end_ns for sp in run) for run in runs)[len(runs) // 2]
    report = await backend.rebuild(RebuildScope(frozenset({b}), since_ns=since), remap, PRICING)
    assert report.runs_scanned == sum(max(sp.end_ns for sp in run) >= since for run in runs)


async def test_rebuild_during_ingestion_converges(db: ContractDB) -> None:
    """REQ-042.b: spans arriving while a rebuild runs end in the same state as a quiet rebuild."""
    backend = db.require_backend()
    runs = dataset(15, 600)
    first = [run[: len(run) // 2] for run in runs]
    rest = [sp for run in runs for sp in run[len(run) // 2 :]]
    project = await load(db, first)

    async def ingest() -> None:
        for i in range(0, len(rest), 25):  # ingest derives with the new mapping too
            batch = tuple(remap(sp) for sp in rest[i : i + 25])
            await backend.write_batch(NormalizedBatch(project, batch))
            await asyncio.sleep(0)

    await asyncio.gather(
        backend.rebuild(RebuildScope(frozenset({project})), remap, PRICING, batch_runs=5),
        ingest(),
    )
    reference = await db.make_project()
    await backend.write_batch(
        NormalizedBatch(reference, tuple(remap(sp) for run in runs for sp in run))
    )
    got, want = await snap(db, project), await snap(db, reference)
    for key in ("runs", "sessions", "llm_calls", "tool_calls", "graph_nodes"):
        assert _drop_ids(got[key]) == _drop_ids(want[key]), key


# ── cost recompute (REQ-062) ─────────────────────────────────────────────────────────────────


async def test_recompute_costs(db: ContractDB) -> None:
    """REQ-062.a: calls in range re-priced with the new version; Runs and Sessions follow."""
    backend = db.require_backend()
    runs = dataset(16, 600)
    project = await load(db, runs)
    v = await backend.save_pricing(
        [
            PricingEntry("gpt-4o*", "glob", 1_000_000_000, 4_000_000_000, "openai"),
            PricingEntry("llama*", "glob", 100_000_000, 100_000_000),
            PricingEntry("claude-sonnet-5-5", "exact", 3_000_000_000, 15_000_000_000),
        ],
        "ana",
    )
    half = TimeRange(BASE_NS, BASE_NS + 15 * 86_400 * NS)
    calls = [sp.llm_call for run in runs for sp in run if sp.llm_call]
    in_half = [c for c in calls if half.start_ns <= c.start_ns < half.end_ns]
    assert await backend.recompute_costs(project, half, v, batch_runs=6) == len(in_half)

    rows = await _llm_rows(db, project)
    for c in calls:
        cost, unpriced, version = rows[c.start_ns, c.model]
        if half.start_ns <= c.start_ns < half.end_ns:
            entry = match_entry(v.entries, c.provider, c.response_model, c.request_model)
            want = llm_cost(entry, c.input_tokens, c.output_tokens, c.cached_input_tokens)
            assert (cost, bool(unpriced), version) == (want, want is None, v.meta.version)
        else:
            assert (cost, bool(unpriced), version) == (c.cost_nusd, c.unpriced, c.pricing_version)
    summaries = await all_runs(db, RunQuery(frozenset({project})))
    total = sum(x[0] or 0 for x in rows.values())
    assert sum(r.cost_nusd or 0 for r in summaries) == total
    with pytest.raises(Exception, match="project"):
        await backend.recompute_costs("01NOPROJECT", half, v)


async def _llm_rows(db: ContractDB, project: str) -> dict[tuple[int, str], tuple[Any, Any, Any]]:
    async with db.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT start_ns, model, cost_nusd, unpriced, pricing_version FROM llm_calls "
                "WHERE project_pk = (SELECT pk FROM projects WHERE id = :p)"
            ),
            {"p": project},
        )
        return {(a, b): (c, d, e) for a, b, c, d, e in rows.all()}
