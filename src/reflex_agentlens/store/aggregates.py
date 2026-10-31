"""Dashboard aggregates in SQL (plan task P2-07.3; Data Model Q8, Q10; REQ-106, DD-005).

Every aggregate is one or two grouped statements whose result has at most (buckets x groups)
rows; nothing iterates over Runs or spans in Python (REQ-106.h). Statements start with
``SELECT`` (no ``WITH``) so the contract suite's rows-fetched recorder sees every one of them.

Scope: Projects resolve to ``project_pk`` and Sources to ``source_pk`` first (tiny lookups), then
both are bound as id sets. Runs are filtered by their own Source; per-span tables (LLM calls,
tool calls, graph nodes, approvals) by the Source of the span that produced them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, and_, or_, select, text

from reflex_agentlens.store import schema as s
from reflex_agentlens.store.dialect import SqlDialectOps, ops_for
from reflex_agentlens.store.records import (
    AggQuery,
    ApprovalDecisionRow,
    ApprovalStats,
    Kpis,
    LatencyDim,
    LatencyRow,
    ModelUsage,
    ModelUsageRow,
    SeriesPoint,
    SourceKey,
    SourceUsageRow,
    TimeSeries,
    ToolStatRow,
)

__all__ = [
    "agg_approvals",
    "agg_by_source",
    "agg_kpis",
    "agg_latency_percentiles",
    "agg_runs_over_time",
    "agg_tokens_cost_by_model",
    "agg_tool_stats",
]


@dataclass(frozen=True, slots=True)
class AggScope:
    """Resolved scope: bound parameters plus SQL predicates for runs and per-span tables."""

    ops: SqlDialectOps
    params: dict[str, Any]
    has_sources: bool
    empty: bool

    def runs(self, alias: str = "r") -> str:
        sql = f"{alias}.project_pk IN ({self.ops.id_set_sql('proj')}) "
        sql += f"AND {alias}.start_ns >= :t0 AND {alias}.start_ns < :t1"
        if self.has_sources:
            sql += f" AND {alias}.source_pk IN ({self.ops.id_set_sql('srcs')})"
        return sql

    def per_span(self, alias: str, time_col: str = "start_ns") -> tuple[str, str]:
        """(join, where) for a per-span table ``alias`` keyed by ``span_pk``."""
        where = f"{alias}.project_pk IN ({self.ops.id_set_sql('proj')}) "
        where += f"AND {alias}.{time_col} >= :t0 AND {alias}.{time_col} < :t1"
        join = ""
        if self.has_sources:
            join = f" JOIN spans sp ON sp.pk = {alias}.span_pk"
            where += f" AND sp.source_pk IN ({self.ops.id_set_sql('srcs')})"
        return join, where


def resolve_scope(conn: Connection, q: AggQuery) -> AggScope:
    ops = ops_for(conn.dialect.name)
    p, src = s.projects, s.sources
    project_pks = (
        list(conn.execute(select(p.c.pk).where(p.c.id.in_(sorted(q.project_ids)))).scalars())
        if q.project_ids
        else []
    )
    params: dict[str, Any] = {
        "proj": ops.id_set_value(project_pks),
        "t0": q.time_range.start_ns,
        "t1": q.time_range.end_ns,
        "b": q.bucket,
    }
    empty = not project_pks
    if q.sources and not empty:
        source_pks = list(
            conn.execute(
                select(src.c.pk).where(
                    src.c.project_pk.in_(project_pks),
                    or_(*[
                        and_(src.c.service_name == k.service_name,
                             src.c.service_namespace == k.service_namespace)
                        for k in q.sources
                    ]),
                )
            ).scalars()
        )  # fmt: skip
        params["srcs"] = ops.id_set_value(source_pks)
        empty = not source_pks
    return AggScope(ops, params, bool(q.sources), empty)


def _flag(column: str) -> str:
    """A boolean column as 0/1 on both backends."""
    return f"CASE WHEN {column} THEN 1 ELSE 0 END"


def _rows(conn: Connection, sql: str, scope: AggScope) -> list[Any]:
    return list(conn.execute(text(sql), scope.params).mappings())


# ── Q8: KPIs ─────────────────────────────────────────────────────────────────────────────────


def agg_kpis(conn: Connection, q: AggQuery) -> Kpis:
    """REQ-106.g: Runs, errors, tokens, priced cost with unpriced count, p95 duration, Sessions."""
    scope = resolve_scope(conn, q)
    if scope.empty:
        return Kpis(0, 0, 0, 0, 0, 0, None, 0)
    totals = _rows(
        conn,
        f"SELECT count(*) AS runs, "  # noqa: S608 - trusted fragments; values are bound
        f"coalesce(sum(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0) AS error_runs, "
        f"coalesce(sum(r.input_tokens), 0) AS it, coalesce(sum(r.output_tokens), 0) AS ot, "
        f"coalesce(sum(r.cost_nusd), 0) AS cost, "
        f"coalesce(sum(CASE WHEN r.unpriced_calls > 0 THEN 1 ELSE 0 END), 0) AS unpriced, "
        f"count(DISTINCT r.session_pk) AS sessions FROM runs r WHERE {scope.runs()}",
        scope,
    )[0]
    p95 = _rows(
        conn,
        scope.ops.percentile_sql(
            [0.95],
            "d",
            [],
            [
                f"SELECT r.duration_ns AS d FROM runs r WHERE {scope.runs()} "  # noqa: S608
                "AND r.duration_ns IS NOT NULL"
            ],
        ),
        scope,
    )
    p95_value = p95[0]["p0"] if p95 else None
    return Kpis(
        runs=int(totals["runs"]),
        error_runs=int(totals["error_runs"]),
        input_tokens=int(totals["it"]),
        output_tokens=int(totals["ot"]),
        cost_nusd=int(totals["cost"]),
        unpriced_runs=int(totals["unpriced"]),
        p95_duration_ns=None if p95_value is None else int(p95_value),
        active_sessions=int(totals["sessions"]),
    )


# ── Q10a: Runs over time by status ───────────────────────────────────────────────────────────


def agg_runs_over_time(conn: Connection, q: AggQuery) -> TimeSeries:
    """REQ-106.a: Run count per (bucket, status)."""
    scope = resolve_scope(conn, q)
    if scope.empty:
        return TimeSeries(q.bucket, [])
    rows = _rows(
        conn,
        f"SELECT (r.start_ns / :b) * :b AS bucket, r.status AS status, count(*) AS n "  # noqa: S608
        f"FROM runs r WHERE {scope.runs()} GROUP BY (r.start_ns / :b) * :b, r.status",
        scope,
    )
    points = sorted(SeriesPoint(int(m["bucket"]), (m["status"],), int(m["n"])) for m in rows)
    return TimeSeries(q.bucket, points)


# ── Q10b: tokens and cost by model ───────────────────────────────────────────────────────────


def _model_rows(conn: Connection, scope: AggScope, bucketed: bool) -> list[ModelUsageRow]:
    join, where = scope.per_span("lc")
    bucket = "(lc.start_ns / :b) * :b" if bucketed else "NULL"
    group = "lc.model, (lc.start_ns / :b) * :b" if bucketed else "lc.model"
    rows = _rows(
        conn,
        f"SELECT lc.model AS model, {bucket} AS bucket, count(*) AS calls, "  # noqa: S608
        f"coalesce(sum(lc.input_tokens), 0) AS it, coalesce(sum(lc.output_tokens), 0) AS ot, "
        f"coalesce(sum(lc.cached_input_tokens), 0) AS ct, coalesce(sum(lc.cost_nusd), 0) AS cost, "
        f"sum({_flag('lc.unpriced')}) AS unpriced "
        f"FROM llm_calls lc{join} WHERE {where} GROUP BY {group}",
        scope,
    )
    out = [
        ModelUsageRow(
            model=m["model"],
            bucket_ns=None if m["bucket"] is None else int(m["bucket"]),
            calls=int(m["calls"]),
            input_tokens=int(m["it"]),
            output_tokens=int(m["ot"]),
            cached_input_tokens=int(m["ct"]),
            cost_nusd=int(m["cost"]),
            unpriced_calls=int(m["unpriced"]),
        )
        for m in rows
    ]
    return sorted(out, key=lambda x: (x.bucket_ns or 0, x.model))


def agg_tokens_cost_by_model(conn: Connection, q: AggQuery) -> ModelUsage:
    """REQ-106.b: per model in total and per bucket; ``unpriced_calls`` flags partial cost."""
    scope = resolve_scope(conn, q)
    if scope.empty:
        return ModelUsage(q.bucket, [], [])
    return ModelUsage(q.bucket, _model_rows(conn, scope, False), _model_rows(conn, scope, True))


# ── Q10c: latency percentiles ────────────────────────────────────────────────────────────────


def agg_latency_percentiles(conn: Connection, q: AggQuery, by: LatencyDim) -> list[LatencyRow]:
    """REQ-106.c: nearest-rank p50/p95/p99 per operation or per graph node (DD-005)."""
    scope = resolve_scope(conn, q)
    if scope.empty:
        return []
    gj, gw = scope.per_span("gn")
    if by == "operation":
        lj, lw = scope.per_span("lc")
        tj, tw = scope.per_span("tc")
        union = (
            f"SELECT lc.operation AS g, lc.duration_ns AS d FROM llm_calls lc{lj} WHERE {lw} "  # noqa: S608
            f"UNION ALL SELECT 'execute_tool' AS g, tc.duration_ns AS d FROM tool_calls tc{tj} "
            f"WHERE {tw} UNION ALL SELECT 'invoke_agent' AS g, gn.end_ns - gn.start_ns AS d "
            f"FROM graph_nodes gn{gj} WHERE {gw} AND gn.node_kind = 'agent'"
        )
    elif by == "node":
        union = (
            f"SELECT gn.node_name AS g, gn.end_ns - gn.start_ns AS d "  # noqa: S608
            f"FROM graph_nodes gn{gj} WHERE {gw}"
        )
    else:
        raise ValueError(f"unknown latency dimension {by!r}")
    source = f"SELECT u.g AS g, u.d AS d FROM ({union}) AS u"  # noqa: S608
    pct = scope.ops.percentile_sql([0.5, 0.95, 0.99], "d", ["g"], [source])
    rows = _rows(
        conn,
        f"SELECT pc.g AS g, c.n AS n, pc.p0 AS p50, pc.p1 AS p95, pc.p2 AS p99 "  # noqa: S608
        f"FROM ({pct}) AS pc JOIN (SELECT u.g AS g, count(*) AS n FROM ({union}) AS u "
        f"GROUP BY u.g) AS c ON c.g = pc.g",
        scope,
    )
    out = [
        LatencyRow(m["g"], int(m["n"]), int(m["p50"]), int(m["p95"]), int(m["p99"])) for m in rows
    ]
    return sorted(out, key=lambda x: x.group)


# ── Q10d: tool statistics ────────────────────────────────────────────────────────────────────


def agg_tool_stats(conn: Connection, q: AggQuery, top_n: int) -> list[ToolStatRow]:
    """REQ-106.d: calls and errors per tool, the ``top_n`` most called (ties by name)."""
    scope = resolve_scope(conn, q)
    if scope.empty or top_n < 1:
        return []
    join, where = scope.per_span("tc")
    name = 'tc.tool_name COLLATE "C"' if scope.ops.name == "postgresql" else "tc.tool_name"
    rows = _rows(
        conn,
        f"SELECT tc.tool_name AS tool, count(*) AS calls, "  # noqa: S608
        f"sum(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) AS errors "
        f"FROM tool_calls tc{join} WHERE {where} GROUP BY tc.tool_name "
        f"ORDER BY count(*) DESC, {name} LIMIT {int(top_n)}",
        scope,
    )
    return [ToolStatRow(m["tool"], int(m["calls"]), int(m["errors"])) for m in rows]


# ── Q10e: cost and tokens by Source ──────────────────────────────────────────────────────────


def agg_by_source(conn: Connection, q: AggQuery) -> list[SourceUsageRow]:
    """REQ-106.e: per Source of the Run's root span (Runs without a root yet are left out)."""
    scope = resolve_scope(conn, q)
    if scope.empty:
        return []
    rows = _rows(
        conn,
        f"SELECT src.service_name AS name, src.service_namespace AS ns, count(*) AS runs, "  # noqa: S608
        f"coalesce(sum(r.input_tokens), 0) AS it, coalesce(sum(r.output_tokens), 0) AS ot, "
        f"coalesce(sum(r.cost_nusd), 0) AS cost, "
        f"sum(CASE WHEN r.unpriced_calls > 0 THEN 1 ELSE 0 END) AS unpriced "
        f"FROM runs r JOIN sources src ON src.pk = r.source_pk WHERE {scope.runs()} "
        f"GROUP BY src.service_name, src.service_namespace",
        scope,
    )
    out = [
        SourceUsageRow(
            source=SourceKey(m["name"], m["ns"]),
            runs=int(m["runs"]),
            input_tokens=int(m["it"]),
            output_tokens=int(m["ot"]),
            cost_nusd=int(m["cost"]),
            unpriced_runs=int(m["unpriced"]),
        )
        for m in rows
    ]
    return sorted(out, key=lambda x: (x.source.service_namespace, x.source.service_name))


# ── Q10f: approvals ──────────────────────────────────────────────────────────────────────────


def agg_approvals(conn: Connection, q: AggQuery) -> ApprovalStats:
    """REQ-106.f: count per decision and nearest-rank median wait (per decision and overall)."""
    scope = resolve_scope(conn, q)
    if scope.empty:
        return ApprovalStats([], None)
    join, where = scope.per_span("ap", "requested_ns")
    base = f"FROM approvals ap{join} WHERE {where}"
    waits = f"SELECT ap.decision AS g, ap.wait_ns AS d {base} AND ap.wait_ns IS NOT NULL"
    per = scope.ops.percentile_sql([0.5], "d", ["g"], [waits])
    rows = _rows(
        conn,
        f"SELECT c.g AS decision, c.n AS n, pc.p0 AS median FROM "  # noqa: S608
        f"(SELECT ap.decision AS g, count(*) AS n {base} GROUP BY ap.decision) AS c "
        f"LEFT JOIN ({per}) AS pc ON pc.g = c.g",
        scope,
    )
    overall = _rows(
        conn,
        scope.ops.percentile_sql(
            [0.5],
            "d",
            [],
            [f"SELECT ap.wait_ns AS d {base} AND ap.wait_ns IS NOT NULL"],
        ),
        scope,
    )
    by_decision = sorted(
        (
            ApprovalDecisionRow(
                m["decision"], int(m["n"]), None if m["median"] is None else int(m["median"])
            )
            for m in rows
        ),
        key=lambda x: x.decision,
    )
    median = overall[0]["p0"] if overall else None
    return ApprovalStats(by_decision, None if median is None else int(median))
