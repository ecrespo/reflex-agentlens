# /// script
# requires-python = ">=3.11"
# dependencies = ["sqlalchemy[asyncio]>=2.0.36", "aiosqlite>=0.22.1", "asyncpg>=0.32.0"]
# ///
"""Spike S2c: query latency and backend parity (Tasks P0-09.1, P0-09.2; Data Model Q1-Q5, Q8, Q10).

Usage:
  uv run --script queries.py --workdir DIR --db s2_100k --suite runs   # P0-09.1 (100k + big trace)
  uv run --script queries.py --workdir DIR --db s2_1000k --suite dash  # P0-09.2 (1M)
Warm cache (one untimed pass first), N timed runs per query, p95 per query and per backend.
Saves EXPLAIN output (SQLite EXPLAIN QUERY PLAN, PG EXPLAIN ANALYZE) and, for the dashboard suite,
checks that both backends return identical values.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sqlalchemy import text  # noqa: E402

import gen  # noqa: E402
import writer  # noqa: E402

DAY = 86_400 * gen.NS
FROM = gen.BASE_NS
TO = gen.BASE_NS + gen.DAYS * DAY


def pctl(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[max(0, math.ceil(len(xs) * p) - 1)]


def percentile_sql(is_pg: bool, p: float, value: str, group: str, source: str) -> str:
    """Nearest-rank percentile (percentile_disc semantics) per group, both dialects (DD-005)."""
    if is_pg:
        return (
            f"SELECT {group}, percentile_disc({p}) WITHIN GROUP (ORDER BY {value}) AS v "
            f"FROM ({source}) src GROUP BY {group}"
        )
    # ceil in double precision, exactly like PG's (int64) ceil(percentile * rowcount)
    x = f"({p} * cnt)"
    return (
        f"SELECT {group}, max(CASE WHEN rn = CAST({x} AS INTEGER) + ({x} > CAST({x} AS INTEGER)) "
        f"THEN {value} END) AS v FROM (SELECT src.*, ROW_NUMBER() OVER (PARTITION BY {group} "
        f"ORDER BY {value}) rn, count(*) OVER (PARTITION BY {group}) cnt FROM ({source}) src) w "
        f"GROUP BY {group}"
    )


def percentiles_sql(
    is_pg: bool, ps: tuple[float, ...], value: str, group: str, sources: list[str]
) -> str:
    """Several nearest-rank percentiles in ONE sort per group (variant tested after the first run).

    PG: ``percentile_disc(ARRAY[...])`` (one ordered-set aggregate). SQLite: one ROW_NUMBER window
    per source stream (groups never span streams), then pick each rank with a conditional max.
    """
    if is_pg:
        arr = ", ".join(str(p) for p in ps)
        picks = ", ".join(f"v[{i + 1}] AS p{i}" for i in range(len(ps)))
        src = " UNION ALL ".join(sources)
        return (
            f"SELECT {group}, {picks} FROM (SELECT {group}, percentile_disc(ARRAY[{arr}]) "
            f"WITHIN GROUP (ORDER BY {value}) v FROM ({src}) s GROUP BY {group}) z"
        )

    def rank(p: float) -> str:
        x = f"({p} * cnt)"
        return f"CAST({x} AS INTEGER) + ({x} > CAST({x} AS INTEGER))"

    picks = ", ".join(
        f"max(CASE WHEN rn = {rank(p)} THEN {value} END) AS p{i}" for i, p in enumerate(ps)
    )
    return " UNION ALL ".join(
        f"SELECT {group}, {picks} FROM (SELECT {group}, {value}, ROW_NUMBER() OVER "
        f"(PARTITION BY {group} ORDER BY {value}) rn, count(*) OVER (PARTITION BY {group}) cnt "
        f"FROM ({s}) s) w GROUP BY {group}"
        for s in sources
    )


def runs_suite(is_pg: bool) -> dict[str, tuple[str, dict]]:
    base = "SELECT r.* FROM runs r WHERE r.project_pk = :p AND r.start_ns >= :f AND r.start_ns < :t"
    order = " ORDER BY r.start_ns DESC, r.pk DESC LIMIT 51"
    prm = {"p": 1, "f": FROM, "t": TO}
    if is_pg:
        attr = (
            " AND EXISTS (SELECT 1 FROM spans s WHERE s.pk = r.root_span_pk "
            "AND s.attributes @> jsonb_build_object(CAST(:k AS text), CAST(:v AS text)))"
        )
        fts = (
            " AND EXISTS (SELECT 1 FROM span_search q WHERE q.run_pk = r.pk "
            "AND q.tsv @@ to_tsquery('simple', :q))"
        )
        q = "upstream & timeout"
    else:
        attr = (
            " AND EXISTS (SELECT 1 FROM spans s WHERE s.pk = r.root_span_pk "
            "AND json_extract(s.attributes, '$.\"' || :k || '\"') = :v)"
        )
        fts = (
            " AND r.pk IN (SELECT s.run_pk FROM span_search f JOIN spans s ON s.pk = f.rowid "
            "WHERE span_search MATCH :q AND s.project_pk = :p)"
        )
        q = '"upstream" "timeout"'
    return {
        "Q1_default_page": (base + order, prm),
        "Q1_keyset_page2": (
            base + " AND (r.start_ns, r.pk) < (:cs, :cp)" + order,
            {**prm, "cs": FROM + 15 * DAY, "cp": 10**12},
        ),
        "Q2_model": (
            base + " AND EXISTS (SELECT 1 FROM llm_calls l WHERE l.project_pk = r.project_pk "
            "AND l.model = :m AND l.run_pk = r.pk)" + order,
            {**prm, "m": "mistral-large"},
        ),
        "Q2_tool": (
            base + " AND EXISTS (SELECT 1 FROM tool_calls t WHERE t.project_pk = r.project_pk "
            "AND t.tool_name = :tool AND t.run_pk = r.pk)" + order,
            {**prm, "tool": "geo-code"},
        ),
        "Q2_status": (base + " AND r.status = :st" + order, {**prm, "st": "error"}),
        "Q2_duration": (
            base + " AND r.duration_ns BETWEEN :d0 AND :d1" + order,
            {**prm, "d0": 60 * gen.NS, "d1": 90 * gen.NS},
        ),
        "Q3_root_attr_eq": (base + attr + order, {**prm, "k": "user.tier", "v": "enterprise"}),
        "Q3_fts_two_terms": (base + fts + order, {**prm, "q": q}),
        "Q5_trace_1000": (
            "SELECT s.pk, s.span_id, s.parent_span_id, s.name, s.category, s.start_ns, s.end_ns, "
            "s.status_code, s.status_message, s.flags, s.source_pk, l.model, l.input_tokens, "
            "l.output_tokens, l.cost_nusd, t.tool_name, t.status, a.decision, g.node_name "
            "FROM spans s LEFT JOIN llm_calls l ON l.span_pk = s.pk "
            "LEFT JOIN tool_calls t ON t.span_pk = s.pk LEFT JOIN approvals a ON a.span_pk = s.pk "
            "LEFT JOIN graph_nodes g ON g.span_pk = s.pk "
            "WHERE s.run_pk = (SELECT pk FROM runs ORDER BY span_count DESC LIMIT 1) "
            "ORDER BY s.start_ns, s.pk",
            {},
        ),
    }


def dash_suite(is_pg: bool) -> dict[str, tuple[str, dict]]:
    scope = "project_pk = :p AND start_ns >= :f AND start_ns < :t"
    prm = {"p": 1, "f": FROM, "t": TO, "b": DAY}
    lat_src = (
        f"SELECT 'op' dim, operation g, duration_ns d FROM llm_calls WHERE {scope} "
        f"UNION ALL SELECT 'op', 'execute_tool', duration_ns FROM tool_calls WHERE {scope} "
        f"UNION ALL SELECT 'op', 'invoke_agent', end_ns - start_ns FROM graph_nodes "
        f"WHERE {scope} AND node_kind = 'agent' "
        f"UNION ALL SELECT 'node', node_name, end_ns - start_ns FROM graph_nodes WHERE {scope}"
    )
    lat = " UNION ALL ".join(
        f"SELECT '{name}' AS pct, dim, g, v FROM ({percentile_sql(is_pg, p, 'd', 'dim, g', lat_src)}) x{i}"
        for i, (name, p) in enumerate((("p50", 0.5), ("p95", 0.95), ("p99", 0.99)))
    )
    p95_runs = percentile_sql(
        is_pg,
        0.95,
        "duration_ns",
        "one",
        f"SELECT 1 AS one, duration_ns FROM runs WHERE {scope} AND duration_ns IS NOT NULL",
    )
    appr_src = (
        "SELECT decision, wait_ns FROM approvals WHERE project_pk = :p "
        "AND requested_ns >= :f AND requested_ns < :t AND wait_ns IS NOT NULL"
    )
    lat_sources = [
        f"SELECT 'op' dim, operation g, duration_ns d FROM llm_calls WHERE {scope}",
        f"SELECT 'op' dim, 'execute_tool' g, duration_ns d FROM tool_calls WHERE {scope}",
        f"SELECT 'op' dim, 'invoke_agent' g, end_ns - start_ns d FROM graph_nodes "
        f"WHERE {scope} AND node_kind = 'agent'",
        f"SELECT 'node' dim, node_name g, end_ns - start_ns d FROM graph_nodes WHERE {scope}",
    ]
    lat_one = percentiles_sql(is_pg, (0.5, 0.95, 0.99), "d", "dim, g", lat_sources)
    queries = {
        "Q8_kpis": (
            f"SELECT count(*) runs, sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END) errors, "
            f"sum(input_tokens + output_tokens) tokens, sum(cost_nusd) cost_nusd, "
            f"sum(CASE WHEN unpriced_calls > 0 THEN 1 ELSE 0 END) unpriced_runs, "
            f"count(DISTINCT session_pk) active_sessions, "
            f"(SELECT v FROM ({p95_runs}) pr) p95_duration FROM runs WHERE {scope}",
            prm,
        ),
        "Q10a_runs_by_status_day": (
            f"SELECT (start_ns / :b) * :b bucket, status, count(*) FROM runs WHERE {scope} "
            f"GROUP BY 1, 2 ORDER BY 1, 2",
            prm,
        ),
        "Q10b_tokens_cost_by_model": (
            f"SELECT model, sum(input_tokens), sum(output_tokens), sum(cached_input_tokens), "
            f"sum(cost_nusd), sum(CASE WHEN unpriced THEN 1 ELSE 0 END) FROM llm_calls "
            f"WHERE {scope} GROUP BY model ORDER BY model",
            prm,
        ),
        "Q10b_cost_by_model_day": (
            f"SELECT (start_ns / :b) * :b bucket, model, sum(cost_nusd) FROM llm_calls "
            f"WHERE {scope} GROUP BY 1, 2 ORDER BY 1, 2",
            prm,
        ),
        "Q10c_latency_percentiles": (f"SELECT * FROM ({lat}) z ORDER BY 1, 2, 3", prm),
        "Q10c_one_pass": (f"SELECT * FROM ({lat_one}) z ORDER BY 1, 2", prm),
        "Q10d_top_tools": (
            f"SELECT tool_name, count(*), sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END) "
            f"FROM tool_calls WHERE {scope} GROUP BY tool_name ORDER BY count(*) DESC, "
            f"tool_name LIMIT 10",
            prm,
        ),
        "Q10e_by_source": (
            f"SELECT s.service_name, sum(r.cost_nusd), sum(r.input_tokens + r.output_tokens), "
            f"sum(CASE WHEN r.unpriced_calls > 0 THEN 1 ELSE 0 END) FROM runs r "
            f"JOIN sources s ON s.pk = r.source_pk WHERE r.{scope.replace(' AND ', ' AND r.')} "
            f"GROUP BY s.service_name ORDER BY 1",
            prm,
        ),
        "Q10f_approvals": (
            f"SELECT a.decision, a.n, m.v FROM (SELECT decision, count(*) n FROM approvals "
            f"WHERE project_pk = :p AND requested_ns >= :f AND requested_ns < :t "
            f"GROUP BY decision) a LEFT JOIN ({percentile_sql(is_pg, 0.5, 'wait_ns', 'decision', appr_src)}) m "
            f"ON m.decision = a.decision ORDER BY 1",
            prm,
        ),
    }
    return queries


def norm(row) -> list:
    """Make values comparable across drivers: PG sum(bigint) is Decimal, booleans vs. 0/1."""
    out = []
    for v in row:
        if isinstance(v, bool):
            v = int(v)
        elif isinstance(v, bytes | memoryview):
            v = bytes(v).hex()
        elif isinstance(v, str) and v[:1] in "[{":
            v = json.loads(v)
        elif isinstance(v, Decimal | float) and v == int(v):
            v = int(v)
        out.append(v)
    return out


async def bench(backend: str, workdir: Path, db: str, suite: str, n: int) -> dict:
    engine = writer.open_engine(backend, db, workdir)
    is_pg = backend == "postgresql"
    queries = runs_suite(is_pg) if suite == "runs" else dash_suite(is_pg)
    res, plans, values = {}, {}, {}
    async with engine.connect() as conn:
        for name, (sql, prm) in queries.items():
            rows = (await conn.execute(text(sql), prm)).all()  # warm-up
            values[name] = [norm(r) for r in rows]
            ex = "EXPLAIN (ANALYZE, BUFFERS) " if is_pg else "EXPLAIN QUERY PLAN "
            plan = (await conn.execute(text(ex + sql), prm)).all()
            plans[name] = "\n".join(str(r[0]) if is_pg else f"{r[1]}|{r[3]}" for r in plan)
            times = []
            for _ in range(n):
                t0 = time.perf_counter()
                (await conn.execute(text(sql), prm)).all()
                times.append((time.perf_counter() - t0) * 1000)
            res[name] = {
                "rows": len(rows),
                "p50_ms": round(pctl(times, 0.5), 1),
                "p95_ms": round(pctl(times, 0.95), 1),
            }
        if suite == "dash":  # whole aggregate set, as the dashboard issues it
            totals = []
            for _ in range(n):
                t0 = time.perf_counter()
                for name, (sql, prm) in queries.items():
                    if name == "Q10c_latency_percentiles":
                        continue  # full set uses the one-pass variant

                    (await conn.execute(text(sql), prm)).all()
                totals.append((time.perf_counter() - t0) * 1000)
            res["_full_set"] = {
                "p50_ms": round(pctl(totals, 0.5), 1),
                "p95_ms": round(pctl(totals, 0.95), 1),
            }
    await engine.dispose()
    (workdir / f"explain-{suite}-{backend}.txt").write_text(
        "\n\n".join(f"== {k}\n{v}" for k, v in plans.items())
    )
    return {"latency": res, "values": values}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--suite", choices=("runs", "dash"), required=True)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--backends", default="sqlite,postgresql")
    args = ap.parse_args()
    out = {}
    for backend in args.backends.split(","):
        out[backend] = await bench(backend, args.workdir, args.db, args.suite, args.n)
    report = {b: v["latency"] for b, v in out.items()}
    if len(out) == 2:
        a, b = out["sqlite"]["values"], out["postgresql"]["values"]
        report["parity"] = {k: a[k] == b[k] for k in a}
        report["parity_diffs"] = {
            k: [(i, x, y) for i, (x, y) in enumerate(zip(a[k], b[k])) if x != y][:2]
            + [len(a[k]), len(b[k])]
            for k in a
            if a[k] != b[k]
        }
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
