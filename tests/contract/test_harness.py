"""Contract harness smoke tests (plan task P2-04.1; REQ-070.b, REQ-106.h)."""

from __future__ import annotations

import pytest

from reflex_agentlens.store import migrate
from reflex_agentlens.store.records import NormalizedBatch, SchemaStatus
from tests.contract.conftest import ContractDB
from tests.contract.dataset import dataset
from tests.contract.rowcount import rows_fetched


async def test_every_backend_starts_migrated_and_empty(db: ContractDB) -> None:
    """REQ-070.b: the same contract suite runs against every backend."""
    assert await migrate.read_schema_status(db.engine) is SchemaStatus.CURRENT
    assert await db.scalar("SELECT count(*) FROM runs") == 0
    project_id = await db.make_project()
    assert await db.scalar("SELECT id FROM projects") == project_id


def test_dataset_is_deterministic_and_rich() -> None:
    a, b = dataset(7, 300), dataset(7, 300)
    assert a == b
    spans = [sp for run in a for sp in run]
    assert len(spans) >= 300
    assert {sp.category for sp in spans} >= {"agent", "llm", "tool", "generic_genai"}
    assert any(sp.llm_call and sp.llm_call.unpriced for sp in spans)
    assert len({sp.trace_id for sp in spans}) == len(a)
    assert dataset(8, 300) != a


async def test_rows_fetched_helper_flags_an_unbounded_query(db: ContractDB) -> None:
    """REQ-106.h: the helper fails on a deliberately unbounded query and passes a bounded one."""
    backend = db.require_backend()
    project_id = await db.make_project()
    spans = tuple(sp for run in dataset(3, 200) for sp in run)
    await backend.write_batch(NormalizedBatch(project_id, spans))

    async with db.engine.connect() as conn:
        with rows_fetched(db.engine) as fetched:
            await conn.exec_driver_sql("SELECT status, count(*) FROM runs GROUP BY status")
        fetched.assert_bounded(4)

        with rows_fetched(db.engine) as fetched:
            await conn.exec_driver_sql("SELECT * FROM spans")  # unbounded on purpose
        assert fetched.max_rows == len(spans)
        with pytest.raises(AssertionError, match="unbounded fetch"):
            fetched.assert_bounded(50)
