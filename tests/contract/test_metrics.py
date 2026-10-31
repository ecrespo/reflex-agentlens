# ruff: noqa: S106  # "token_type" is a metric label, not a password
"""Metric point contract tests (plan task P2-07.4; Data Model §2.16, Q11; REQ-005, REQ-107)."""

from __future__ import annotations

from typing import Any

from reflex_agentlens.store.attrs import attrs_hash
from reflex_agentlens.store.metrics import LEGACY_TOKEN_METRIC, TOKEN_COUNTER_PREFIX
from reflex_agentlens.store.records import (
    HOUR_NS,
    MINUTE_NS,
    AggQuery,
    MetricPoint,
    SeriesPoint,
    SourceKey,
    TimeRange,
)
from tests.contract.conftest import ContractDB
from tests.contract.dataset import BASE_NS

A, B = SourceKey("chat-api", "shop"), SourceKey("batch-job")
T0 = BASE_NS + 3 * HOUR_NS  # minute-aligned


def at(minute: int, second: int = 0) -> int:
    return T0 + minute * MINUTE_NS + second * 1_000_000_000


def point(source: SourceKey, name: str, kind: str, start: int, time: int, **kw: Any) -> MetricPoint:
    attrs = {"gen_ai.request.model": kw.get("model"), "series": kw.pop("series", "x")}
    return MetricPoint(
        project_id=kw.pop("project_id"),
        source=source,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        start_ns=start,
        time_ns=time,
        attrs_hash=attrs_hash(attrs),
        attributes=attrs,
        **kw,
    )


def scenario(project: str) -> list[MetricPoint]:
    counter_in = TOKEN_COUNTER_PREFIX + "input_tokens"
    counter_out = TOKEN_COUNTER_PREFIX + "output_tokens"
    common = {"project_id": project}
    return [
        # Source A, delta counter: 100 then 50 input tokens of gpt-4o
        point(A, counter_in, "sum", at(0), at(1), model="gpt-4o", token_type="input",
              temporality=1, is_monotonic=True, value_int=100, **common),
        point(A, counter_in, "sum", at(1), at(2), model="gpt-4o", token_type="input",
              temporality=1, is_monotonic=True, value_int=50, **common),
        # Source A, cumulative counter 10 → 30 → 35, then a reset (new start) at 7
        point(A, counter_out, "sum", at(0), at(1), model="claude", token_type="output",
              temporality=2, is_monotonic=True, value_int=10, **common),
        point(A, counter_out, "sum", at(0), at(2), model="claude", token_type="output",
              temporality=2, is_monotonic=True, value_int=30, **common),
        point(A, counter_out, "sum", at(0), at(2, 30), model="claude", token_type="output",
              temporality=2, is_monotonic=True, value_int=35, **common),
        point(A, counter_out, "sum", at(3), at(4), model="claude", token_type="output",
              temporality=2, is_monotonic=True, value_int=7, **common),
        # Source A also sends the legacy histogram: ignored, the counters win for this Source
        point(A, LEGACY_TOKEN_METRIC, "histogram", at(0), at(1), model="gpt-4o",
              token_type="input", temporality=1, count=2, sum=999.0, **common),
        # Source B only has the legacy histogram: 12.6 + 7.4 input tokens of gpt-4o-mini
        point(B, LEGACY_TOKEN_METRIC, "histogram", at(0), at(1), model="gpt-4o-mini",
              token_type="input", temporality=1, count=3, sum=12.6, **common),
        point(B, LEGACY_TOKEN_METRIC, "histogram", at(1), at(1, 40), model="gpt-4o-mini",
              token_type="input", temporality=1, count=1, sum=7.4, **common),
        # another gen_ai metric: stored, not counted as tokens
        point(B, "gen_ai.client.operation.duration", "histogram", at(0), at(1), model="x",
              temporality=1, count=3, sum=1.5, **common),
    ]  # fmt: skip


async def test_token_usage_by_model_over_time(db: ContractDB) -> None:
    """REQ-107.a: counters preferred, legacy histogram otherwise, cumulative → deltas."""
    project = await db.make_project()
    backend = db.require_backend()
    assert await backend.write_metric_points(scenario(project)) == 10
    q = AggQuery(frozenset({project}), TimeRange(T0, T0 + HOUR_NS))
    ts = await backend.agg_metric_token_usage(q)
    assert ts.bucket_ns == MINUTE_NS
    assert ts.points == sorted([
        SeriesPoint(at(1), ("gpt-4o", "input"), 100),
        SeriesPoint(at(2), ("gpt-4o", "input"), 50),
        SeriesPoint(at(1), ("claude", "output"), 10),
        SeriesPoint(at(2), ("claude", "output"), 25),  # 30-10 and 35-30 share the minute
        SeriesPoint(at(4), ("claude", "output"), 7),  # reset: a new series starts at 7
        SeriesPoint(at(1), ("gpt-4o-mini", "input"), 20),  # 12.6 + 7.4
    ])  # fmt: skip
    only_b = await backend.agg_metric_token_usage(
        AggQuery(frozenset({project}), q.time_range, sources=(B,))
    )
    assert only_b.points == [SeriesPoint(at(1), ("gpt-4o-mini", "input"), 20)]
    assert (await backend.agg_metric_token_usage(AggQuery(frozenset(), q.time_range))).points == []


async def test_metric_points_are_idempotent(db: ContractDB) -> None:
    """REQ-005: a re-exported point is ignored; Sources are created on first sight."""
    project = await db.make_project()
    backend = db.require_backend()
    points = scenario(project)
    assert await backend.write_metric_points(points + points[:2]) == 10  # in-batch duplicates
    assert await backend.write_metric_points(points) == 0
    assert await backend.write_metric_points([]) == 0
    assert await db.scalar("SELECT count(*) FROM metric_points") == 10
    sources = await backend.list_sources(frozenset({project}))
    assert {v.source for v in sources} == {A, B}
