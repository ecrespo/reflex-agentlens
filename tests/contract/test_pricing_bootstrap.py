"""Bundled pricing bootstrap against both backends (plan task P3-05.1; Data Model §5, A-11)."""

from __future__ import annotations

from reflex_agentlens.ingest.cost import bootstrap_pricing, load_bundled_pricing
from tests.contract.conftest import ContractDB


async def test_bootstrap_saves_version_one_once(db: ContractDB) -> None:
    backend = db.require_backend()
    seed = load_bundled_pricing()
    assert await bootstrap_pricing(backend, seed)
    assert not await bootstrap_pricing(backend, seed)
    (meta,) = await backend.list_pricing_versions()
    assert (meta.version, meta.source, meta.effective_from_ns) == (1, "bundled", 0)
    assert meta.note == seed.note
    stored = await backend.get_pricing(0)
    assert len(stored.entries) == len(seed.entries)
