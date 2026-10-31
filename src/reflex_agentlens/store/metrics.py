"""GenAI metric points (plan task P2-07.4; Data Model §2.16, Q11; REQ-005, REQ-107).

Token usage reads the new ``gen_ai.client.inference.usage.*`` counters when a Source exports
them in the range, and the legacy ``gen_ai.client.token.usage`` histogram otherwise, so a client
that emits both is not counted twice. Cumulative series become deltas in SQL with ``LAG()``
over the series (Source, metric, attributes, start time); a new start time is a counter reset.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import groupby
from typing import Any

from sqlalchemy import Connection, text

from reflex_agentlens.store import schema as s
from reflex_agentlens.store.aggregates import resolve_scope
from reflex_agentlens.store.dialect import ops_for
from reflex_agentlens.store.records import AggQuery, MetricPoint, SeriesPoint, TimeSeries
from reflex_agentlens.store.writepath import MAX_BIND_PARAMS, project_pk_of, upsert_sources

__all__ = [
    "LEGACY_TOKEN_METRIC",
    "TOKEN_COUNTER_PREFIX",
    "agg_metric_token_usage",
    "write_metric_points",
]

TOKEN_COUNTER_PREFIX = "gen_ai.client.inference.usage."  # noqa: S105 - metric name
LEGACY_TOKEN_METRIC = "gen_ai.client.token.usage"  # noqa: S105 - metric name
_KEY = ("source_pk", "name", "attrs_hash", "start_ns", "time_ns")


def _row(pt: MetricPoint, project_pk: int, source_pk: int) -> dict[str, Any]:
    return {
        "project_pk": project_pk,
        "source_pk": source_pk,
        "name": pt.name,
        "kind": pt.kind,
        "unit": pt.unit,
        "is_monotonic": pt.is_monotonic,
        "temporality": pt.temporality,
        "start_ns": pt.start_ns,
        "time_ns": pt.time_ns,
        "attrs_hash": pt.attrs_hash,
        "attributes": pt.attributes,
        "model": pt.model,
        "token_type": pt.token_type,
        "value_int": pt.value_int,
        "value_double": pt.value_double,
        "count": pt.count,
        "sum": pt.sum,
        "bucket_counts": pt.bucket_counts,
        "explicit_bounds": pt.explicit_bounds,
    }


def write_metric_points(conn: Connection, points: Sequence[MetricPoint]) -> int:
    """Insert points idempotently (re-exports are ignored); returns how many were new."""
    ops = ops_for(conn.dialect.name)
    t = s.metric_points
    inserted = 0
    ordered = sorted(points, key=lambda pt: pt.project_id)
    for project_id, group in groupby(ordered, key=lambda pt: pt.project_id):
        batch = list(group)
        project_pk = project_pk_of(conn, project_id)
        sources = upsert_sources(
            conn, ops, project_pk, ((pt.source, pt.start_ns, pt.time_ns) for pt in batch)
        )
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for pt in batch:
            row = _row(pt, project_pk, sources[pt.source])
            rows.setdefault(tuple(row[k] for k in _KEY), row)  # first copy wins
        values = list(rows.values())
        size = max(1, MAX_BIND_PARAMS // len(t.columns))
        for i in range(0, len(values), size):
            stmt = (
                ops.insert(t)
                .values(values[i : i + size])
                .on_conflict_do_nothing(index_elements=[t.c.project_pk, *(t.c[k] for k in _KEY)])
                .returning(t.c.pk)
            )
            inserted += len(conn.execute(stmt).all())
    return inserted


def agg_metric_token_usage(conn: Connection, q: AggQuery) -> TimeSeries:
    """Q11: tokens per (bucket, model, token type) from metric points (REQ-107.a)."""
    scope = resolve_scope(conn, q)
    if scope.empty:
        return TimeSeries(q.bucket, [])
    scope.params["prefix"] = TOKEN_COUNTER_PREFIX + "%"
    scope.params["legacy"] = LEGACY_TOKEN_METRIC
    in_scope = f"m.project_pk IN ({scope.ops.id_set_sql('proj')}) "
    in_scope += "AND m.time_ns >= :t0 AND m.time_ns < :t1"
    if scope.has_sources:
        in_scope += f" AND m.source_pk IN ({scope.ops.id_set_sql('srcs')})"
    raw = "CASE WHEN m.kind = 'histogram' THEN m.sum ELSE m.value_int END"
    series = "PARTITION BY m.source_pk, m.name, m.attrs_hash, m.start_ns ORDER BY m.time_ns"
    picked = (
        f"SELECT m.time_ns AS time_ns, coalesce(m.model, 'unknown') AS model, "  # noqa: S608
        f"coalesce(m.token_type, 'unknown') AS token_type, "
        f"CASE WHEN m.temporality = 2 THEN {raw} - coalesce(LAG({raw}) OVER ({series}), 0) "
        f"ELSE {raw} END AS v FROM metric_points m WHERE {in_scope} AND "
        f"((m.kind = 'sum' AND m.name LIKE :prefix) OR (m.kind = 'histogram' AND m.name = :legacy "
        f"AND NOT EXISTS (SELECT 1 FROM metric_points n WHERE n.project_pk = m.project_pk "
        f"AND n.source_pk = m.source_pk AND n.name LIKE :prefix "
        f"AND n.time_ns >= :t0 AND n.time_ns < :t1)))"
    )
    rows = conn.execute(
        text(
            f"SELECT (x.time_ns / :b) * :b AS bucket, x.model AS model, "  # noqa: S608
            f"x.token_type AS token_type, CAST(round(sum(x.v)) AS BIGINT) AS tokens "
            f"FROM ({picked}) AS x GROUP BY (x.time_ns / :b) * :b, x.model, x.token_type"
        ),
        scope.params,
    ).mappings()
    points = sorted(
        SeriesPoint(int(m["bucket"]), (m["model"], m["token_type"]), int(m["tokens"])) for m in rows
    )
    return TimeSeries(q.bucket, points)
