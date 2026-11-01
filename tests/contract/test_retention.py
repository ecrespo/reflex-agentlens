"""Retention and job-lock contract tests (plan task P2-09.1; Data Model Q13, §2.18).

REQ-090.a/b (age), REQ-091.a/b (size, <= 90 %, settings never purged), REQ-092.b (single
holder), REQ-092.c (bounded batches), REQ-173 (usage, last purge).
"""

from __future__ import annotations

from typing import Any

import pytest

from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.security import generate_api_key, hash_secret
from reflex_agentlens.store import retention
from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.records import (
    NewApiKeyRecord,
    NewSavedFilter,
    ProjectPatch,
    PurgeReport,
    RunQuery,
    SessionQuery,
)
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.dataset import NS, dataset
from tests.contract.test_reads import all_runs, load
from tests.contract.test_sessions import all_sessions

DAY = 86_400 * NS
RUNS = dataset(17, 1200)


def last_span(run: list[Any]) -> int:
    return max(sp.end_ns for sp in run)


async def counts(db: ContractDB) -> dict[str, object]:
    return {
        t: await db.scalar(f"SELECT count(*) FROM {t}")  # noqa: S608 - fixed table names
        for t in ("runs", "spans", "llm_calls", "tool_calls", "graph_nodes", "approvals")
    }


async def test_purge_by_age(db: ContractDB) -> None:
    """REQ-090.b: whole traces older than the Project's max age; Sessions recomputed."""
    backend = db.require_backend()
    aged, forever = await load(db, RUNS), await load(db, RUNS)
    await backend.update_project(forever, ProjectPatch(retention_max_age_s=0))  # REQ-090.a
    key = generate_api_key()
    h = hash_secret(key.secret)
    await backend.create_api_key(NewApiKeyRecord(key.key_id, aged, h.salt, h.digest, NOW_NS))
    await backend.save_filter(NewSavedFilter(new_ulid(), aged, "errors", "status=error"))

    cutoff = NOW_NS - 30 * DAY
    expired = [run for run in RUNS if last_span(run) < cutoff]
    assert 5 < len(expired) < len(RUNS)
    report = await backend.purge_by_age(NOW_NS, batch_traces=4, trigger="manual")
    assert (report.deleted_traces_age, report.deleted_traces_size) == (len(expired), 0)
    assert report.deleted_bytes_est == sum(sp.size_bytes for run in expired for sp in run)
    assert (report.trigger, report.error_class, report.started_ns) == ("manual", None, NOW_NS)

    left = await all_runs(db, RunQuery(frozenset({aged})))
    assert {r.trace_id for r in left} == {run[0].trace_id for run in RUNS} - {
        run[0].trace_id for run in expired
    }
    assert len(await all_runs(db, RunQuery(frozenset({forever})))) == len(RUNS)
    kept = [run for run in RUNS if run not in expired]
    total_spans = sum(len(run) for run in RUNS) + sum(len(run) for run in kept)
    assert (await counts(db))["spans"] == total_spans
    for x in await all_sessions(db, SessionQuery(frozenset({aged}))):
        assert x.run_count == sum(r.session_id == x.session_id for r in left)
    orphans = await db.scalar("SELECT count(*) FROM sessions WHERE run_count = 0")
    assert orphans == 0

    # REQ-091.b: settings are never purged
    assert await backend.resolve_api_key(key.key_id) is not None
    assert len(await backend.list_saved_filters(aged)) == 1
    assert await backend.last_purge() == report
    again = await backend.purge_by_age(NOW_NS, batch_traces=4)
    assert again.deleted_traces_age == 0


async def test_count_purgeable_matches_the_purge_and_deletes_nothing(db: ContractDB) -> None:
    """REQ-092.a ``purge --dry-run`` (P5-03.2): the count the next purge by age deletes."""
    backend = db.require_backend()
    aged, forever = await load(db, RUNS), await load(db, RUNS)
    await backend.update_project(forever, ProjectPatch(retention_max_age_s=0))
    expired = [run for run in RUNS if last_span(run) < NOW_NS - 30 * DAY]
    before = await counts(db)
    assert await backend.count_purgeable(NOW_NS) == len(expired)
    assert await counts(db) == before
    assert await backend.last_purge() is None
    report = await backend.purge_by_age(NOW_NS, batch_traces=50)
    assert report.deleted_traces_age == len(expired)
    assert await backend.count_purgeable(NOW_NS) == 0
    assert len(await all_runs(db, RunQuery(frozenset({aged})))) == len(RUNS) - len(expired)


async def test_purge_by_size(db: ContractDB) -> None:
    """REQ-091.a: oldest traces across Projects until usage <= 90 % of the limit."""
    backend = db.require_backend()
    await load(db, RUNS)
    await load(db, RUNS[:20])
    usage = (await backend.storage_usage()).estimated_bytes
    assert (await backend.purge_by_size(usage, 10)).deleted_traces_size == 0  # at the limit

    limit = usage // 2
    report = await backend.purge_by_size(limit, batch_traces=7)
    after = (await backend.storage_usage()).estimated_bytes
    assert after <= limit * 9 // 10
    assert report.deleted_bytes_est == usage - after
    assert report.deleted_traces_size > 0
    oldest_left = await db.scalar("SELECT min(last_span_ns) FROM runs")
    assert isinstance(oldest_left, int)
    # nothing older than what is left was kept: deleted Runs are a prefix in last-span order
    lasts = sorted([last_span(run) for run in RUNS] + [last_span(run) for run in RUNS[:20]])
    assert lasts.index(oldest_left) == report.deleted_traces_size


async def test_storage_usage(db: ContractDB) -> None:
    """REQ-173.a: estimated bytes, physical bytes, Run and span counts."""
    backend = db.require_backend()
    await load(db, RUNS)
    u = await backend.storage_usage()
    assert u.estimated_bytes == sum(sp.size_bytes for run in RUNS for sp in run)
    assert u.physical_bytes is not None
    assert u.physical_bytes > 0
    assert (u.runs, u.spans) == (len(RUNS), sum(len(run) for run in RUNS))


async def test_purge_failure_is_logged_and_raised(
    db: ContractDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = db.require_backend()

    def boom(*_: Any) -> Any:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(retention, "age_batch", boom)
    with pytest.raises(RuntimeError):
        await backend.purge_by_age(NOW_NS, 10)
    last = await backend.last_purge()
    assert last is not None
    assert last.error_class == "RuntimeError"  # class name only, never the message


async def test_purge_log_is_trimmed(db: ContractDB) -> None:
    report = PurgeReport("scheduled", 1, 2, 0, 0, 0)
    async with db.engine.begin() as conn:
        for _ in range(retention.PURGE_LOG_KEEP + 3):
            await conn.run_sync(retention.record_purge, report)
    assert await db.scalar("SELECT count(*) FROM purge_log") == retention.PURGE_LOG_KEEP


async def test_job_lock_single_holder(db: ContractDB) -> None:
    """REQ-092.b: a second holder gets False until the first releases."""
    backend = db.require_backend()
    async with backend.job_lock("retention") as first:
        assert first
        async with backend.job_lock("retention") as second:
            assert not second
        async with backend.job_lock("finalizer") as other:
            assert other  # locks are per job name
    async with backend.job_lock("retention") as again:
        assert again


@pytest.mark.pg
async def test_job_lock_across_processes(db: ContractDB) -> None:
    """REQ-092.b on PostgreSQL: two backends (two processes) share the advisory lock."""
    if db.name != "pg":
        pytest.skip("cross-process locks are a PostgreSQL concern")
    assert isinstance(db.backend, PostgresBackend)
    second = PostgresBackend(db.backend.url, search_path=db.backend.search_path, pool_size=1)
    await second.open()
    try:
        async with db.backend.job_lock("retention") as mine:
            assert mine
            async with second.job_lock("retention") as theirs:
                assert not theirs
        async with second.job_lock("retention") as theirs:
            assert theirs
    finally:
        await second.close()


async def test_huge_max_age_does_not_overflow(db: ContractDB) -> None:
    """A maximum age near BIGINT's range must not break the purge for every Project."""
    backend = db.require_backend()
    huge, normal = await load(db, RUNS), await load(db, RUNS)
    await backend.update_project(huge, ProjectPatch(retention_max_age_s=2**62))
    report = await backend.purge_by_age(NOW_NS, batch_traces=50)
    expired = [run for run in RUNS if last_span(run) < NOW_NS - 30 * DAY]
    assert report.deleted_traces_age == len(expired)  # only ``normal`` lost traces
    assert len(await all_runs(db, RunQuery(frozenset({huge})))) == len(RUNS)
    assert len(await all_runs(db, RunQuery(frozenset({normal})))) == len(RUNS) - len(expired)
