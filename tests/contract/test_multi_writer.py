"""Multi-writer contract test on PostgreSQL (plan task P2-06.2; REQ-072.b, Analyze A-01, DD-004).

Two backends on the same database (``serve`` + ``collect`` style) write interleaved batches of the
**same** traces, duplicates included, concurrently. Afterwards the Project's derived views must
equal a single-writer ingest of the same span set.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.records import NormalizedBatch, NormalizedSpan
from tests.contract.conftest import ContractDB
from tests.contract.dataset import dataset
from tests.contract.snapshot import snapshot

pytestmark = pytest.mark.pg
SPANS = [sp for run in dataset(21, 400) for sp in run]
ROUNDS = 20


def _batches(spans: list[NormalizedSpan], rng: random.Random) -> list[tuple[NormalizedSpan, ...]]:
    shuffled = spans + rng.sample(spans, len(spans) // 5)  # ~20 % duplicates
    rng.shuffle(shuffled)
    out, i = [], 0
    while i < len(shuffled):
        n = rng.randint(5, 60)
        out.append(tuple(shuffled[i : i + n]))
        i += n
    return out


async def _writer(
    backend: PostgresBackend, project: str, batches: list[tuple[NormalizedSpan, ...]]
) -> int:
    inserted = 0
    for batch in batches:
        inserted += (await backend.write_batch(NormalizedBatch(project, batch))).inserted
    return inserted


async def test_concurrent_writers_equal_a_single_writer(db: ContractDB) -> None:
    """REQ-072.b: interleaved writers of the same traces converge; no deadlock escapes the retry."""
    if db.name != "pg":
        pytest.skip("multi-writer concurrency is a PostgreSQL concern (SQLite has one writer)")
    assert isinstance(db.backend, PostgresBackend)
    reference = await db.make_project()
    await db.backend.write_batch(NormalizedBatch(reference, tuple(SPANS)))
    async with db.engine.connect() as conn:
        expected = await conn.run_sync(snapshot, reference)

    second = PostgresBackend(
        db.backend.url, search_path=db.backend.search_path, clock=db.clock, pool_size=2
    )
    await second.open()
    try:
        retries = 0
        for round_ in range(ROUNDS):
            rng = random.Random(round_)  # noqa: S311 - test schedule
            project = await db.make_project()
            batches = _batches(SPANS, rng)
            a, b = batches[0::2], batches[1::2]
            inserted = await asyncio.gather(
                _writer(db.backend, project, a), _writer(second, project, b)
            )
            assert sum(inserted) == len(SPANS), f"round {round_}"
            async with db.engine.connect() as conn:
                got = await conn.run_sync(snapshot, project)
            assert got == expected, f"round {round_}"
            retries = db.backend.deadlock_retries + second.deadlock_retries
    finally:
        await second.close()
    print(f"deadlock retries over {ROUNDS} rounds: {retries}")  # noqa: T201 - test diagnostics
