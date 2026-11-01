"""``write_group`` and ``get_or_create_secret`` contract tests (plan task P3-06.1).

API v1.3 §5.1, Tech Design DD-003 (Analyze A-04), Data Model §2.1 (Analyze A-08), REQ-009.a.
"""

from __future__ import annotations

import dataclasses

import pytest

from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.records import (
    BatchRejected,
    NormalizedBatch,
    RunKey,
    RunQuery,
    WriteResult,
)
from reflex_agentlens.store.sqlite.backend import SqliteBackend
from tests.contract.conftest import ContractDB
from tests.contract.test_changefeed import nothing_more, receive, trace
from tests.contract.test_reads import all_runs
from tests.contract.test_write_batch import span


def batch(project: str, t: int, **kw: object) -> NormalizedBatch:
    return NormalizedBatch(project, (span(t, 1, None, 10, 5, **kw),))


async def test_deleted_project_rejects_only_its_batch(db: ContractDB) -> None:
    """A-04: a key still in the auth cache for a deleted Project must not sink the group."""
    backend = db.require_backend()
    a, gone, c = await db.make_project(), await db.make_project(), await db.make_project()
    await backend.delete_project(gone)
    async with backend.changes.subscribe() as sub:
        results = await backend.write_group([batch(a, 1), batch(gone, 2), batch(c, 3)])
        assert isinstance(results[0], WriteResult)
        assert results[1] == BatchRejected("project_not_found")
        assert isinstance(results[2], WriteResult)
        assert (results[0].inserted, results[2].inserted) == (1, 1)
        assert results[0].affected_runs == [RunKey(a, trace(1))]
        got = {(ev.project_id, ev.run_keys) for ev in (await receive(sub), await receive(sub))}
        assert got == {(a, (RunKey(a, trace(1)),)), (c, (RunKey(c, trace(3)),))}
        await nothing_more(sub)
    runs = await all_runs(db, RunQuery(frozenset({a, c})))
    assert sorted(r.trace_id for r in runs) == [trace(1), trace(3)]


async def test_constraint_violation_rolls_back_only_that_batch(db: ContractDB) -> None:
    backend = db.require_backend()
    project = await db.make_project()
    good, bad = batch(project, 1), batch(project, 2)
    bad = dataclasses.replace(
        bad, spans=(dataclasses.replace(bad.spans[0], category="bogus"),)  # type: ignore[arg-type]
    )
    results = await backend.write_group([good, bad, batch(project, 3)])
    assert results[1] == BatchRejected("constraint_violation")
    runs = await all_runs(db, RunQuery(frozenset({project})))
    assert sorted(r.trace_id for r in runs) == [trace(1), trace(3)]  # trace 2 left nothing behind


async def test_too_long_value_is_invalid_data_on_postgres(db: ContractDB) -> None:
    if db.name != "pg":
        pytest.skip("SQLite does not enforce TEXT(n) widths")
    backend = db.require_backend()
    project = await db.make_project()
    results = await backend.write_group(
        [batch(project, 1, agent_name="a" * 300), batch(project, 2)]
    )
    assert results[0] == BatchRejected("invalid_data")
    assert isinstance(results[1], WriteResult)


async def test_empty_group_and_duplicates(db: ContractDB) -> None:
    backend = db.require_backend()
    assert await backend.write_group([]) == []
    project = await db.make_project()
    first, again = await backend.write_group([batch(project, 1), batch(project, 1)])
    assert isinstance(first, WriteResult)
    assert isinstance(again, WriteResult)
    assert (first.inserted, again.inserted, again.duplicates) == (1, 0, 1)


async def test_other_errors_fail_the_whole_group(
    db: ContractDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = db.require_backend()
    project = await db.make_project()
    calls = 0

    def explode(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise OSError("disk gone")

    async with backend.changes.subscribe() as sub:
        monkeypatch.setattr("reflex_agentlens.store.writeapi.write_batch", explode)
        with pytest.raises(OSError, match="disk gone"):
            await backend.write_group([batch(project, 1), batch(project, 2)])
        monkeypatch.undo()
        await nothing_more(sub)
    assert calls == 1
    assert await all_runs(db, RunQuery(frozenset({project}))) == []


# ── app_meta secret (A-08) ────────────────────────────────────────────────────────────────────


async def test_secret_is_created_once_and_shared(db: ContractDB) -> None:
    backend = db.require_backend()
    first = await backend.get_or_create_secret("session_key")
    assert len(first) == 32
    assert await backend.get_or_create_secret("session_key") == first
    if isinstance(backend, PostgresBackend):
        other: PostgresBackend | SqliteBackend = PostgresBackend(
            backend.url, search_path=backend.search_path, pool_size=2
        )
    else:
        assert isinstance(backend, SqliteBackend)
        other = SqliteBackend(backend.path)
    await other.open()
    try:
        assert await other.get_or_create_secret("session_key") == first
    finally:
        await other.close()
