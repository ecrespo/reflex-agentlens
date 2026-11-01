"""Golden normalizer fixtures (plan task P3-12.2; REQ-032.c, REQ-035.b, REQ-036.c, REQ-131).

Each ``fixtures/<name>.json`` holds hand-authored OTLP/JSON exports (one ``write_group`` each, in
order). They go through the real path (decode → ``normalize_batch`` → ``write_group`` →
finalizer) on SQLite with a fixed clock, and the derived views must equal
``expected/<name>.json``.

Intentional mapping changes: run with ``AGENTLENS_REGEN_GOLDEN=1`` and review the diff. The
expected files record ``MAPPING_VERSION``; bumping it without regenerating fails the suite.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from reflex_agentlens.core.semconv.mapping import MAPPING_VERSION
from reflex_agentlens.ingest.cost import PricingSnapshot
from reflex_agentlens.ingest.decode import decode_traces
from reflex_agentlens.ingest.normalize.pipeline import BatchContext, normalize_batch
from reflex_agentlens.ingest.redaction.rules import compile_rules
from reflex_agentlens.store.records import (
    NewProject,
    PricingEntry,
    RedactionRuleSet,
    WriteResult,
)
from reflex_agentlens.store.sqlite.backend import SqliteBackend
from tests.contract.snapshot import snapshot
from tests.unit.ingest import otlp

HERE = Path(__file__).parent
FIXTURES = sorted((HERE / "fixtures").glob("*.json"))
REGEN = os.environ.get("AGENTLENS_REGEN_GOLDEN") == "1"
NOW = otlp.NOW_NS
WINDOW_NS = 5 * 60 * 1_000_000_000
PRICES = [
    PricingEntry("gpt-4o*", "glob", 2_500_000_000, 10_000_000_000),
    PricingEntry("claude-*", "glob", 3_000_000_000, 15_000_000_000),
]


def jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


async def derived_views(fixture: dict[str, Any], db_path: Path) -> dict[str, Any]:
    backend = SqliteBackend(db_path, clock=lambda: NOW)
    await backend.open()
    try:
        project = await backend.create_project(
            NewProject("01GOLDEN000000000000000000", "golden", "G")
        )
        await backend.save_pricing(PRICES, "golden", effective_from_ns=0)
        ctx = BatchContext(
            project_id=project.id,
            received_at_ns=NOW,
            rules=compile_rules(RedactionRuleSet({"secrets": True}, [], [])),
            session_secret=b"g" * 32,
            pricing=PricingSnapshot([await backend.get_pricing(0)]),
        )
        rejected = 0
        for doc in fixture["requests"]:
            request = decode_traces(json.dumps(doc).encode(), "json")
            result = normalize_batch(request, ctx)
            rejected += result.rejections.total
            (outcome,) = await backend.write_group([result.batch])
            assert isinstance(outcome, WriteResult)
        await backend.finalize_stale_runs(NOW - WINDOW_NS, 1_000)
        async with backend.write_engine.connect() as conn:
            views = await conn.run_sync(snapshot, project.id)
        return {"rejected_spans": rejected, **jsonable(views)}
    finally:
        await backend.close()


def _read(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
async def test_golden(path: Path, sqlite_template: Path, tmp_path: Path) -> None:
    fixture = _read(path)
    db_path = tmp_path / "golden.db"
    shutil.copyfile(sqlite_template, db_path)
    actual = {"mapping_version": MAPPING_VERSION, "views": await derived_views(fixture, db_path)}
    expected_path = HERE / "expected" / path.name
    if REGEN:
        expected_path.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
        return
    assert expected_path.exists(), (
        f"no golden file: run with AGENTLENS_REGEN_GOLDEN=1 ({path.stem})"
    )
    expected = json.loads(expected_path.read_text())
    assert expected["mapping_version"] == MAPPING_VERSION, (
        "MAPPING_VERSION changed: regenerate the golden files with AGENTLENS_REGEN_GOLDEN=1 "
        "and review the diff"
    )
    assert actual == expected


def test_every_fixture_has_a_golden_file() -> None:
    expected = {p.stem for p in (HERE / "expected").glob("*.json")}
    assert expected == {p.stem for p in FIXTURES}
