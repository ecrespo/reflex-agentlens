"""Spike S2b: store write path, Data Model Q6 + Q7 on SQLite and PostgreSQL (P0-07.2, P0-08.x).

``write_group`` is one group commit: upsert sources/resources/sessions, insert Run stubs
(insert-or-ignore), insert spans with ``ON CONFLICT DO NOTHING RETURNING``, insert derived rows and
search documents only for returned spans, then recompute every affected Run from scratch (Q7) and
the affected Sessions. Single writer (DD-003); the PG multi-writer locking of Analyze A-01 is out of
scope here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import event, select, text
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

import schema as S
from gen import MAPPING_VERSION, SOURCES, SpanRec

PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-65536",
)
PG_URL = "postgresql+asyncpg://postgres:spike@127.0.0.1:55432/{db}"
IN_PROGRESS_WINDOW_NS = 30 * 60 * 1_000_000_000


# --- engines and schema ----------------------------------------------------------------------


def sqlite_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        for p in PRAGMAS:
            cur.execute(p)
        cur.close()

    return engine


async def fresh(backend: str, name: str, workdir: Path) -> AsyncEngine:
    """Create an empty database with the schema and one Project; return its engine."""
    if backend == "sqlite":
        path = workdir / f"{name}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        engine = sqlite_engine(path)
        async with engine.begin() as c:
            await c.exec_driver_sql("PRAGMA page_size=8192")
            await c.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
        async with engine.connect() as c:
            await c.exec_driver_sql("PRAGMA journal_mode=WAL")
    else:
        admin = create_async_engine(PG_URL.format(db="postgres"), isolation_level="AUTOCOMMIT")
        async with admin.connect() as c:
            await c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await c.exec_driver_sql(f'CREATE DATABASE "{name}"')
        await admin.dispose()
        engine = create_async_engine(PG_URL.format(db=name), pool_size=5)
    async with engine.begin() as c:
        await c.run_sync(S.metadata.create_all)
        await c.run_sync(S.create_search)
        await c.execute(  # first identity value is 1 on both backends
            S.projects.insert().values(
                id="01JPROJECT0000000000000001",
                slug="default",
                name="Default",
                created_at_ns=0,
                updated_at_ns=0,
            )
        )
        await c.execute(
            S.pricing_versions.insert().values(
                version=1, effective_from_ns=0, source="bundled", created_at_ns=0
            )
        )
    return engine


def open_engine(backend: str, name: str, workdir: Path) -> AsyncEngine:
    if backend == "sqlite":
        return sqlite_engine(workdir / f"{name}.db")
    return create_async_engine(PG_URL.format(db=name), pool_size=5)


# --- write path ------------------------------------------------------------------------------


def _ins(conn: AsyncConnection, table):
    mod = sqlite_dialect if conn.dialect.name == "sqlite" else pg_dialect
    return mod.insert(table)


def _doc_size(s: SpanRec) -> int:
    return len(s.name) + len(json.dumps(s.attributes)) + len(s.status_message or "")


class Caches:
    """Per-writer natural-key caches (sources, resources, sessions), as the real writer keeps."""

    def __init__(self) -> None:
        self.sources: dict[int, int] = {}
        self.resources: dict[int, int] = {}


async def write_group(
    conn: AsyncConnection, spans: list[SpanRec], now_ns: int, caches: Caches, project_pk: int = 1
) -> dict:
    is_pg = conn.dialect.name == "postgresql"
    # 1. sources + resources (one resource per source in this dataset)
    for src in sorted({s.source for s in spans} - caches.sources.keys()):
        ns_, name = SOURCES[src]
        t = min(s.start_ns for s in spans if s.source == src)
        await conn.execute(
            _ins(conn, S.sources)
            .values(
                project_pk=project_pk,
                service_namespace=ns_,
                service_name=name,
                first_seen_ns=t,
                last_seen_ns=t,
            )
            .on_conflict_do_nothing()
        )
        caches.sources[src] = (
            await conn.execute(
                select(S.sources.c.pk).where(
                    S.sources.c.project_pk == project_pk,
                    S.sources.c.service_namespace == ns_,
                    S.sources.c.service_name == name,
                )
            )
        ).scalar_one()
        attrs = {"service.name": name, "service.namespace": ns_}
        h = hashlib.blake2b(json.dumps(attrs, sort_keys=True).encode(), digest_size=16).digest()
        await conn.execute(
            _ins(conn, S.resources)
            .values(project_pk=project_pk, attrs_hash=h, attributes=attrs)
            .on_conflict_do_nothing()
        )
        caches.resources[src] = (
            await conn.execute(
                select(S.resources.c.pk).where(
                    S.resources.c.project_pk == project_pk, S.resources.c.attrs_hash == h
                )
            )
        ).scalar_one()

    # 2. sessions: insert-or-ignore by natural key (aggregates come from the recompute)
    sess = {}
    for s in spans:
        if s.session_ext and s.session_ext not in sess:
            sess[s.session_ext] = s.start_ns
    if sess:
        await conn.execute(
            _ins(conn, S.sessions).on_conflict_do_nothing(),
            [
                {
                    "id": hashlib.blake2b(e.encode(), digest_size=13).hexdigest()[:26].upper(),
                    "project_pk": project_pk,
                    "external_id": e,
                    "first_ns": t,
                    "last_ns": t,
                    "updated_at_ns": now_ns,
                }
                for e, t in sorted(sess.items())
            ],
        )

    # 3. Q6: run stubs, then resolve pks
    first = {}
    for s in spans:
        f = first.get(s.trace_id)
        if f is None or s.start_ns < f[0]:
            first[s.trace_id] = (s.start_ns, max(s.end_ns, f[1] if f else 0))
    trace_ids = sorted(first)
    await conn.execute(
        _ins(conn, S.runs).on_conflict_do_nothing(),
        [
            {
                "project_pk": project_pk,
                "trace_id": t,
                "start_ns": first[t][0],
                "last_span_ns": first[t][1],
                "status": "in_progress",
                "updated_at_ns": now_ns,
            }
            for t in trace_ids
        ],
    )
    run_pk = dict(
        (
            await conn.execute(
                select(S.runs.c.trace_id, S.runs.c.pk).where(
                    S.runs.c.project_pk == project_pk, S.runs.c.trace_id.in_(trace_ids)
                )
            )
        ).all()
    )

    # 4. spans insert-or-ignore RETURNING
    by_key = {(run_pk[s.trace_id], s.span_id): s for s in spans}
    rows = [
        {
            "project_pk": project_pk,
            "run_pk": rp,
            "span_id": sid,
            "parent_span_id": s.parent_span_id,
            "source_pk": caches.sources[s.source],
            "resource_pk": caches.resources[s.source],
            "name": s.name,
            "category": s.category,
            "is_genai": s.is_genai,
            "start_ns": s.start_ns,
            "end_ns": s.end_ns,
            "status_code": s.status_code,
            "status_message": s.status_message,
            "attributes": s.attributes,
            "agent_name": s.agent_name,
            "session_ext": s.session_ext,
            "agent_usage_input": s.agent_usage_input,
            "agent_usage_output": s.agent_usage_output,
            "mapping_version": MAPPING_VERSION,
            "size_bytes": _doc_size(s),
            "received_at_ns": now_ns,
            "otel_kind": 1,
            "flags": 0,
        }
        for (rp, sid), s in by_key.items()
    ]
    stmt = (
        _ins(conn, S.spans)
        .on_conflict_do_nothing(index_elements=["run_pk", "span_id"])
        .returning(S.spans.c.pk, S.spans.c.run_pk, S.spans.c.span_id)
    )
    returned = (await conn.execute(stmt, rows)).all() if rows else []

    # 5. derived rows + search docs for returned spans only
    llm, tool, appr, node, docs = [], [], [], [], []
    for pk, rp, sid in returned:
        s = by_key[(rp, sid)]
        base = {"span_pk": pk, "run_pk": rp, "project_pk": project_pk}
        if s.llm:
            llm.append(
                {**base, **s.llm, "start_ns": s.start_ns, "duration_ns": s.end_ns - s.start_ns}
            )
        if s.tool:
            tool.append(
                {**base, **s.tool, "start_ns": s.start_ns, "duration_ns": s.end_ns - s.start_ns}
            )
        if s.approval:
            appr.append({**base, **s.approval})
        if s.node:
            node.append({**base, **s.node, "start_ns": s.start_ns, "end_ns": s.end_ns})
        docs.append({"pk": pk, "run_pk": rp, "project_pk": project_pk, "doc": s.doc})
    for table, data in (
        (S.llm_calls, llm),
        (S.tool_calls, tool),
        (S.approvals, appr),
        (S.graph_nodes, node),
    ):
        if data:
            await conn.execute(table.insert(), data)
    if docs:
        if is_pg:
            await conn.execute(
                text(
                    "INSERT INTO span_search (span_pk, run_pk, project_pk, tsv) "
                    "VALUES (:pk, :run_pk, :project_pk, to_tsvector('simple', :doc))"
                ),
                docs,
            )
        else:
            await conn.execute(
                text("INSERT INTO span_search (rowid, doc) VALUES (:pk, :doc)"),
                [{"pk": d["pk"], "doc": d["doc"]} for d in docs],
            )

    # 6. Q7 recompute for affected runs, then sessions
    affected = sorted({rp for _, rp, _ in returned})
    if affected:
        await recompute(conn, affected, now_ns)
    return {"sent": len(spans), "inserted": len(returned), "runs": len(affected)}


# --- Q7 ----------------------------------------------------------------------------------------


def _q7(is_pg: bool) -> str:
    aff = (
        "SELECT unnest(CAST(:pks AS BIGINT[])) AS run_pk"
        if is_pg
        else "SELECT value AS run_pk FROM json_each(:pks)"
    )
    models = (
        "SELECT run_pk, jsonb_agg(DISTINCT model ORDER BY model) AS models "
        "FROM llm_calls WHERE run_pk IN (SELECT run_pk FROM affected) GROUP BY run_pk"
        if is_pg
        else "SELECT run_pk, json_group_array(model) AS models FROM (SELECT DISTINCT run_pk, model "
        "FROM llm_calls WHERE run_pk IN (SELECT run_pk FROM affected) ORDER BY run_pk, model) "
        "GROUP BY run_pk"
    )
    return f"""
WITH affected AS ({aff}),
s AS (SELECT sp.pk, sp.run_pk, sp.span_id, sp.parent_span_id, sp.name, sp.category, sp.start_ns,
             sp.end_ns, sp.status_code, sp.is_genai, sp.size_bytes, sp.source_pk, sp.agent_name,
             sp.session_ext, sp.agent_usage_input, sp.agent_usage_output
      FROM spans sp WHERE sp.run_pk IN (SELECT run_pk FROM affected)),
root AS (SELECT * FROM (SELECT s.*, ROW_NUMBER() OVER (PARTITION BY run_pk ORDER BY start_ns, pk) rn
                        FROM s WHERE parent_span_id IS NULL) x WHERE rn = 1),
first_any AS (SELECT * FROM (SELECT run_pk, source_pk,
                 ROW_NUMBER() OVER (PARTITION BY run_pk ORDER BY start_ns, pk) rn FROM s) x
              WHERE rn = 1),
first_sess AS (SELECT * FROM (SELECT run_pk, session_ext,
                 ROW_NUMBER() OVER (PARTITION BY run_pk ORDER BY start_ns, pk) rn FROM s
                 WHERE session_ext IS NOT NULL) x WHERE rn = 1),
first_agent AS (SELECT * FROM (SELECT run_pk, agent_name,
                 ROW_NUMBER() OVER (PARTITION BY run_pk ORDER BY start_ns, pk) rn FROM s
                 WHERE category = 'agent' AND agent_name IS NOT NULL) x WHERE rn = 1),
agent_usage AS (SELECT * FROM (SELECT run_pk, agent_usage_input, agent_usage_output,
                 ROW_NUMBER() OVER (PARTITION BY run_pk ORDER BY start_ns, pk) rn FROM s
                 WHERE category = 'agent' AND (agent_usage_input IS NOT NULL
                       OR agent_usage_output IS NOT NULL)) x WHERE rn = 1),
agg AS (SELECT run_pk, min(start_ns) start_ns, max(end_ns) last_span_ns, count(*) span_count,
               sum(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) error_spans,
               max(CASE WHEN is_genai THEN 1 ELSE 0 END) is_genai, sum(size_bytes) bytes_est,
               count(DISTINCT source_pk) source_count
        FROM s GROUP BY run_pk),
orphans AS (SELECT c.run_pk, count(*) n FROM s c WHERE c.parent_span_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM s p WHERE p.run_pk = c.run_pk
                            AND p.span_id = c.parent_span_id) GROUP BY c.run_pk),
llm AS (SELECT run_pk, count(*) n, sum(coalesce(input_tokens, 0)) it,
               sum(coalesce(output_tokens, 0)) ot, sum(coalesce(cached_input_tokens, 0)) ct,
               sum(coalesce(cache_write_tokens, 0)) cwt, sum(coalesce(reasoning_tokens, 0)) rt,
               sum(cost_nusd) cost, sum(CASE WHEN unpriced THEN 1 ELSE 0 END) unpriced_calls
        FROM llm_calls WHERE run_pk IN (SELECT run_pk FROM affected) GROUP BY run_pk),
models AS ({models}),
tools AS (SELECT run_pk, count(*) n FROM tool_calls
          WHERE run_pk IN (SELECT run_pk FROM affected) GROUP BY run_pk),
calc AS (
  SELECT agg.run_pk, agg.start_ns, agg.last_span_ns, agg.span_count, agg.error_spans,
         agg.is_genai, agg.bytes_est, agg.source_count,
         root.pk root_pk, root.name root_name, root.end_ns root_end, root.status_code root_status,
         coalesce(root.agent_name, first_agent.agent_name) agent_name,
         coalesce(root.source_pk, first_any.source_pk) source_pk,
         coalesce(root.session_ext, first_sess.session_ext) session_ext,
         coalesce(orphans.n, 0) missing_parents,
         coalesce(llm.n, 0) llm_n, llm.it, llm.ot, llm.ct, llm.cwt, llm.rt, llm.cost,
         coalesce(llm.unpriced_calls, 0) unpriced_calls, models.models,
         coalesce(tools.n, 0) tool_n,
         agent_usage.agent_usage_input aui, agent_usage.agent_usage_output auo
  FROM agg LEFT JOIN root ON root.run_pk = agg.run_pk
  LEFT JOIN first_any ON first_any.run_pk = agg.run_pk
  LEFT JOIN first_sess ON first_sess.run_pk = agg.run_pk
  LEFT JOIN first_agent ON first_agent.run_pk = agg.run_pk
  LEFT JOIN agent_usage ON agent_usage.run_pk = agg.run_pk
  LEFT JOIN orphans ON orphans.run_pk = agg.run_pk
  LEFT JOIN llm ON llm.run_pk = agg.run_pk LEFT JOIN models ON models.run_pk = agg.run_pk
  LEFT JOIN tools ON tools.run_pk = agg.run_pk)
UPDATE runs SET
  start_ns = calc.start_ns, last_span_ns = calc.last_span_ns, span_count = calc.span_count,
  error_spans = calc.error_spans, is_genai = (calc.is_genai = 1), bytes_est = calc.bytes_est,
  source_count = calc.source_count, root_span_pk = calc.root_pk, root_name = calc.root_name,
  end_ns = calc.root_end,
  duration_ns = CASE WHEN calc.root_pk IS NOT NULL THEN calc.root_end - calc.start_ns END,
  status = CASE WHEN calc.root_pk IS NOT NULL
                THEN CASE WHEN calc.root_status = 2 THEN 'error' ELSE 'ok' END
                WHEN calc.last_span_ns > :cutoff_ns THEN 'in_progress'
                ELSE 'incomplete' END,
  agent_name = calc.agent_name, source_pk = calc.source_pk,
  missing_parents = calc.missing_parents, llm_calls = calc.llm_n, tool_calls = calc.tool_n,
  usage_source = CASE WHEN calc.llm_n > 0 THEN 'llm'
                      WHEN calc.aui IS NOT NULL OR calc.auo IS NOT NULL THEN 'agent'
                      ELSE 'none' END,
  input_tokens = CASE WHEN calc.llm_n > 0 THEN calc.it ELSE coalesce(calc.aui, 0) END,
  output_tokens = CASE WHEN calc.llm_n > 0 THEN calc.ot ELSE coalesce(calc.auo, 0) END,
  cached_input_tokens = coalesce(calc.ct, 0), cache_write_tokens = coalesce(calc.cwt, 0),
  reasoning_tokens = coalesce(calc.rt, 0), cost_nusd = calc.cost,
  unpriced_calls = calc.unpriced_calls,
  models = coalesce(calc.models, {"'[]'::jsonb" if is_pg else "'[]'"}),
  session_pk = (SELECT ss.pk FROM sessions ss WHERE ss.project_pk = runs.project_pk
                AND ss.external_id = calc.session_ext),
  updated_at_ns = :now_ns
FROM calc WHERE runs.pk = calc.run_pk
"""


def _sessions_sql(is_pg: bool) -> str:
    aff = (
        "SELECT unnest(CAST(:pks AS BIGINT[])) AS pk"
        if is_pg
        else "SELECT value AS pk FROM json_each(:pks)"
    )
    bool_expr = "(x.unpriced = 1)" if is_pg else "x.unpriced"
    return f"""
WITH aff AS ({aff}),
x AS (SELECT aff.pk, count(r.pk) n, min(r.start_ns) first_ns,
             max(coalesce(r.end_ns, r.last_span_ns)) last_ns,
             coalesce(sum(r.input_tokens), 0) it, coalesce(sum(r.output_tokens), 0) ot,
             coalesce(sum(r.cached_input_tokens), 0) ct, coalesce(sum(r.cost_nusd), 0) cost,
             coalesce(max(CASE WHEN r.unpriced_calls > 0 THEN 1 ELSE 0 END), 0) unpriced
      FROM aff LEFT JOIN runs r ON r.session_pk = aff.pk GROUP BY aff.pk)
UPDATE sessions SET run_count = x.n, first_ns = coalesce(x.first_ns, sessions.first_ns),
  last_ns = coalesce(x.last_ns, sessions.last_ns), input_tokens = x.it, output_tokens = x.ot,
  cached_input_tokens = x.ct, cost_nusd = x.cost, includes_unpriced = {bool_expr},
  updated_at_ns = :now_ns
FROM x WHERE sessions.pk = x.pk
"""


async def recompute(conn: AsyncConnection, run_pks: list[int], now_ns: int) -> None:
    is_pg = conn.dialect.name == "postgresql"
    pks = run_pks if is_pg else json.dumps(run_pks)

    async def session_pks() -> set[int]:
        rows = await conn.execute(
            select(S.runs.c.session_pk).where(S.runs.c.pk.in_(run_pks)).distinct()
        )
        return {r[0] for r in rows if r[0] is not None}

    before = await session_pks()
    await conn.execute(
        text(_q7(is_pg)),
        {"pks": pks, "now_ns": now_ns, "cutoff_ns": now_ns - IN_PROGRESS_WINDOW_NS},
    )
    touched = sorted(before | await session_pks())
    if touched:
        await conn.execute(
            text(_sessions_sql(is_pg)),
            {"pks": touched if is_pg else json.dumps(touched), "now_ns": now_ns},
        )
