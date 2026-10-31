"""``write_batch`` contract tests (plan task P2-05.2; API §5.1; Data Model Q6, Q7).

REQ-031, REQ-032, REQ-036, REQ-037, REQ-038, REQ-073.a, DD-004, DD-012.
"""

from __future__ import annotations

import dataclasses
import itertools
import random
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from reflex_agentlens.core.errors import NotFound
from reflex_agentlens.store.records import (
    LLMCallRow,
    NormalizedBatch,
    NormalizedSpan,
    RunKey,
    SessionKey,
    SourceKey,
)
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.dataset import NS, dataset, resource_for
from tests.contract.snapshot import snapshot

WINDOW = 5 * 60 * NS
SRC = SourceKey("svc")


def span(
    trace: int, sid: int, parent: int | None, start: int, end: int, **kw: Any
) -> NormalizedSpan:
    """A minimal span; times are offsets in seconds before NOW."""
    base: dict[str, Any] = {
        "trace_id": trace.to_bytes(16, "big"),
        "span_id": sid.to_bytes(8, "big"),
        "parent_span_id": None if parent is None else parent.to_bytes(8, "big"),
        "source": SRC,
        "resource": resource_for(SRC),
        "name": f"span {sid}",
        "start_ns": NOW_NS - start * NS,
        "end_ns": NOW_NS - end * NS,
        "mapping_version": 1,
        "size_bytes": 100,
        "received_at_ns": NOW_NS,
    }
    return NormalizedSpan(**(base | kw))


async def write(db: ContractDB, project_id: str, *spans: NormalizedSpan) -> Any:
    return await db.require_backend().write_batch(NormalizedBatch(project_id, tuple(spans)))


async def snap(db: ContractDB, project_id: str) -> dict[str, Any]:
    async with db.engine.connect() as conn:
        return await conn.run_sync(snapshot, project_id)


async def run_row(db: ContractDB, project_id: str, trace: int = 1) -> dict[str, Any]:
    runs = (await snap(db, project_id))["runs"]
    return next(r for r in runs if r["trace_id"] == trace.to_bytes(16, "big"))


# ── REQ-038: duplicates ──────────────────────────────────────────────────────────────────────


async def test_duplicates_are_counted_and_not_stored_twice(db: ContractDB) -> None:
    """REQ-038.a: same (Project, trace, span) → first version kept, counted, views unchanged."""
    project = await db.make_project()
    first = span(1, 1, None, 60, 10, name="original")
    res = await write(db, project, first, span(1, 2, 1, 50, 20))
    assert (res.inserted, res.duplicates) == (2, 0)
    before = await snap(db, project)

    changed = dataclasses.replace(first, name="second version", status_code=2)
    res = await write(db, project, changed, span(1, 2, 1, 50, 20))
    assert (res.inserted, res.duplicates, res.affected_runs) == (0, 2, [])
    after = await snap(db, project)
    assert after == before
    assert "original" in str(after["spans"])
    assert "second version" not in str(after["spans"])


async def test_duplicates_inside_one_batch_keep_the_first(db: ContractDB) -> None:
    project = await db.make_project()
    a = span(1, 1, None, 60, 10, name="first")
    res = await write(db, project, a, dataclasses.replace(a, name="later"))
    assert (res.inserted, res.duplicates) == (1, 1)
    assert (await snap(db, project))["spans"] == [((1).to_bytes(8, "big"), "first")]


async def test_same_span_id_in_another_project_is_not_a_duplicate(db: ContractDB) -> None:
    p1, p2 = await db.make_project("one"), await db.make_project("two")
    s = span(1, 1, None, 60, 10)
    assert (await write(db, p1, s)).inserted == 1
    assert (await write(db, p2, s)).inserted == 1


# ── REQ-032: Run status and fields ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spans", "status"),
    [
        ([span(1, 2, 1, 60, 30)], "in_progress"),  # no root, last span 30 s ago < window
        ([span(1, 2, 1, 900, 600)], "incomplete"),  # no root, last span 10 min ago
        ([span(1, 1, None, 60, 10, status_code=1)], "ok"),
        ([span(1, 1, None, 60, 10, status_code=0)], "ok"),  # UNSET is not ERROR
        ([span(1, 1, None, 60, 10, status_code=2)], "error"),
        ([span(1, 1, None, 900, 600, status_code=2)], "error"),  # root wins over the window
    ],
)
async def test_run_status_rules(db: ContractDB, spans: list[NormalizedSpan], status: str) -> None:
    """REQ-032.b with the injected clock (window 5 min)."""
    project = await db.make_project()
    await write(db, project, *spans)
    assert (await run_row(db, project))["status"] == status


async def test_incomplete_run_recovers_when_the_root_arrives(db: ContractDB) -> None:
    """REQ-032.c and REQ-037.c: a late span recomputes the Run and its status."""
    project = await db.make_project()
    await write(db, project, span(1, 2, 1, 900, 600))
    assert (await run_row(db, project))["status"] == "incomplete"
    await write(db, project, span(1, 1, None, 1000, 500, status_code=2, name="root"))
    row = await run_row(db, project)
    assert (row["status"], row["root_name"]) == ("error", "root")
    assert row["duration_ns"] == 500 * NS
    assert row["end_ns"] == NOW_NS - 500 * NS


async def test_run_aggregates(db: ContractDB) -> None:
    """REQ-032.a/d: totals, counts, models, genai flag and agent name from the stored spans."""
    project = await db.make_project()
    llm = LLMCallRow(start_ns=0, duration_ns=1, operation="chat", unpriced=False)
    await write(
        db,
        project,
        span(1, 1, None, 100, 10, category="agent", agent_name="triage", is_genai=True),
        span(1, 2, 1, 90, 80, category="llm", is_genai=True, status_code=2,
             llm_call=dataclasses.replace(llm, model="gpt-4o", input_tokens=10, output_tokens=5,
                                          cached_input_tokens=2, cost_nusd=700)),
        span(1, 3, 1, 70, 60, category="llm",
             llm_call=dataclasses.replace(llm, model="claude", input_tokens=1, output_tokens=1,
                                          unpriced=True)),
        span(1, 4, 9, 50, 40),  # orphan: parent 9 never arrives
    )  # fmt: skip
    row = await run_row(db, project)
    assert row["span_count"] == 4
    assert (row["llm_calls"], row["tool_calls"], row["error_spans"]) == (2, 0, 1)
    assert (row["input_tokens"], row["output_tokens"], row["cached_input_tokens"]) == (11, 6, 2)
    assert (row["cost_nusd"], row["unpriced_calls"]) == (700, 1)
    assert row["models"] == ["claude", "gpt-4o"]
    assert row["usage_source"] == "llm"
    assert row["is_genai"] is True or row["is_genai"] == 1
    assert row["agent_name"] == "triage"
    assert row["missing_parents"] == 1
    assert row["bytes_est"] == 400
    assert row["start_ns"] == NOW_NS - 100 * NS
    assert row["last_span_ns"] == NOW_NS - 10 * NS


async def test_cost_is_null_when_every_call_is_unpriced(db: ContractDB) -> None:
    """Data Model §2.9: NULL cost (not zero) when all LLM calls are unpriced (REQ-061.c)."""
    project = await db.make_project()
    llm = LLMCallRow(start_ns=0, duration_ns=1, operation="chat", unpriced=True, input_tokens=3)
    await write(db, project, span(1, 1, None, 9, 1, llm_call=llm))
    row = await run_row(db, project)
    assert (row["cost_nusd"], row["unpriced_calls"], row["input_tokens"]) == (None, 1, 3)


async def test_agent_usage_is_a_fallback_only(db: ContractDB) -> None:
    """DD-012: agent-level usage counts only when the trace has no LLM call rows."""
    project = await db.make_project()
    agent = {"category": "agent", "agent_usage_input": 1000, "agent_usage_output": 100}
    await write(db, project, span(1, 1, None, 9, 1, **agent))
    row = await run_row(db, project)
    assert (row["usage_source"], row["input_tokens"], row["output_tokens"]) == ("agent", 1000, 100)

    llm = LLMCallRow(start_ns=0, duration_ns=1, operation="chat", unpriced=False,
                     input_tokens=7, output_tokens=3)  # fmt: skip
    await write(db, project, span(1, 2, 1, 8, 2, llm_call=llm))
    row = await run_row(db, project)
    assert (row["usage_source"], row["input_tokens"], row["output_tokens"]) == ("llm", 7, 3)


async def test_orphans_are_relinked_when_the_parent_arrives(db: ContractDB) -> None:
    """REQ-037.b: the child is stored at once; ``missing_parents`` drops when the parent lands."""
    project = await db.make_project()
    await write(db, project, span(1, 3, 2, 50, 40))
    assert (await run_row(db, project))["missing_parents"] == 1
    await write(db, project, span(1, 2, 1, 60, 30))
    assert (await run_row(db, project))["missing_parents"] == 1  # 2's parent (1) still missing
    await write(db, project, span(1, 1, None, 70, 20))
    assert (await run_row(db, project))["missing_parents"] == 0


# ── REQ-031 / REQ-036: Sources and Sessions ──────────────────────────────────────────────────


async def test_sources_by_natural_key(db: ContractDB) -> None:
    """REQ-031.a/b: Sources keyed by (namespace, name) per Project, created on first sight."""
    project = await db.make_project()
    a, b, c = SourceKey("svc"), SourceKey("svc", "shop"), SourceKey("unknown_service")
    await write(
        db, project,
        span(1, 1, None, 90, 10, source=a, resource=resource_for(a)),
        span(1, 2, 1, 80, 70, source=b, resource=resource_for(b)),
        span(2, 1, None, 60, 50, source=c, resource=resource_for(c)),
    )  # fmt: skip
    await write(db, project, span(3, 1, None, 5, 1, source=a, resource=resource_for(a)))
    snap_ = await snap(db, project)
    assert [(ns, name) for ns, name, *_ in snap_["sources"]] == [
        ("", "svc"), ("", "unknown_service"), ("shop", "svc"),
    ]  # fmt: skip
    assert snap_["sources"][0][2:] == (NOW_NS - 90 * NS, NOW_NS - 1 * NS)  # first/last seen
    run1 = next(r for r in snap_["runs"] if r["trace_id"] == (1).to_bytes(16, "big"))
    assert (run1["source_name"], run1["source_ns"], run1["source_count"]) == ("svc", "", 2)


async def test_session_totals(db: ContractDB) -> None:
    """REQ-036.a/b: Sessions created on first sight; run count, times, tokens, cost, unpriced."""
    project = await db.make_project()
    conv = SessionKey("conv-1")
    priced = LLMCallRow(
        start_ns=0,
        duration_ns=1,
        operation="chat",
        unpriced=False,
        input_tokens=10,
        output_tokens=1,
        cached_input_tokens=4,
        cost_nusd=50,
    )
    unpriced = dataclasses.replace(priced, unpriced=True, cost_nusd=None, input_tokens=5)
    await write(db, project, span(1, 1, None, 100, 90, session=conv, llm_call=priced))
    await write(db, project, span(2, 1, None, 80, 70, session=conv, llm_call=priced))
    (session,) = (await snap(db, project))["sessions"]
    assert (session["run_count"], session["input_tokens"], session["output_tokens"]) == (2, 20, 2)
    assert (session["cached_input_tokens"], session["cost_nusd"]) == (8, 100)
    assert not session["includes_unpriced"]
    assert (session["first_ns"], session["last_ns"]) == (NOW_NS - 100 * NS, NOW_NS - 70 * NS)

    await write(db, project, span(3, 1, None, 60, 50, session=conv, llm_call=unpriced))
    (session,) = (await snap(db, project))["sessions"]
    assert (session["run_count"], session["input_tokens"], session["cost_nusd"]) == (3, 25, 100)
    assert session["includes_unpriced"]  # fmt: skip


async def test_root_session_wins_over_earlier_spans(db: ContractDB) -> None:
    """REQ-036.c: the root span's session ID, else the earliest span's; others stay raw."""
    project = await db.make_project()
    await write(db, project, span(1, 2, 1, 90, 80, session=SessionKey("early")))
    assert (await run_row(db, project))["session_ext"] == "early"
    await write(db, project, span(1, 1, None, 95, 10, session=SessionKey("root-conv")))
    snap_ = await snap(db, project)
    assert snap_["runs"][0]["session_ext"] == "root-conv"
    assert [x["external_id"] for x in snap_["sessions"]] == ["root-conv"]  # "early" now has 0 runs
    assert snap_["session_ids"] == ["early", "root-conv"]


# ── REQ-073.a: search documents ──────────────────────────────────────────────────────────────


async def test_search_documents_for_new_spans_only(db: ContractDB) -> None:
    project = await db.make_project()
    doc = "execute_tool RefundTool — Café"
    await write(db, project, span(1, 1, None, 9, 1, search_doc=doc))
    await write(db, project, span(1, 1, None, 9, 1, search_doc=doc))  # duplicate: no second doc
    if db.name == "sqlite":
        sql = 'SELECT count(*) FROM span_search WHERE span_search MATCH \'"refundtool" "cafe"\''
    else:
        sql = (
            "SELECT count(*) FROM span_search "
            "WHERE tsv @@ to_tsquery('simple', 'refundtool & cafe')"
        )
    assert await db.scalar(sql) == 1


async def test_unknown_project_is_rejected(db: ContractDB) -> None:
    with pytest.raises(NotFound):
        await write(db, "01J" + "0" * 23, span(1, 1, None, 9, 1))


async def test_empty_batch_is_a_no_op(db: ContractDB) -> None:
    project = await db.make_project()
    res = await db.require_backend().write_batch(NormalizedBatch(project, ()))
    assert (res.inserted, res.duplicates, res.affected_runs) == (0, 0, [])


async def test_large_batch_is_chunked_and_affected_runs_are_sorted(db: ContractDB) -> None:
    """Q6: multi-row inserts stay under the bind-parameter limit; keys sorted (DD-004)."""
    project = await db.make_project()
    runs = dataset(11, 3000)
    spans = tuple(sp for run in runs for sp in run)
    res = await write(db, project, *spans)
    assert (res.inserted, res.duplicates) == (len(spans), 0)
    assert res.affected_runs == sorted(RunKey(project, run[0].trace_id) for run in runs)
    assert await db.scalar("SELECT count(*) FROM runs") == len(runs)


# ── REQ-037.a: order and batching invariance ─────────────────────────────────────────────────

SPANS = tuple(sp for run in dataset(5, 120) for sp in run)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(seed=st.integers(0, 2**32 - 1), cuts=st.lists(st.integers(1, len(SPANS) - 1), max_size=6))
async def test_any_order_and_batching_gives_identical_views(
    db: ContractDB, seed: int, cuts: list[int]
) -> None:
    """REQ-037.a: identical Run, Session, LLMCall, ToolCall, Approval for any order and batching."""
    reference = await db.make_project()
    await write(db, reference, *SPANS)

    shuffled = list(SPANS)
    random.Random(seed).shuffle(shuffled)  # noqa: S311 - test permutation
    bounds = [0, *sorted(set(cuts)), len(shuffled)]
    other = await db.make_project()
    for lo, hi in itertools.pairwise(bounds):
        if lo < hi:
            await write(db, other, *shuffled[lo:hi])

    assert await snap(db, other) == await snap(db, reference)
