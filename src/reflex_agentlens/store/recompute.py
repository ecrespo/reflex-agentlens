"""Run and Session recompute from scratch, in SQL (Data Model Q7; DD-004; REQ-032, 036, 037).

Every derived Run field is a pure function of the stored spans and derived rows of its trace, so
the result does not depend on arrival order or batching (REQ-037.a). Ties between spans with the
same start time are broken by ``span_id`` (part of the input), never by ``pk`` (arrival order).

Ported from spike S2 (``spikes/s2/writer.py``), validated there on 1M spans on both backends.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Connection, select, text

from reflex_agentlens.store import schema as s
from reflex_agentlens.store.dialect import ops_for

__all__ = ["recompute_runs", "recompute_sessions"]

_FIRST = "ROW_NUMBER() OVER (PARTITION BY run_pk ORDER BY start_ns, span_id)"


def _runs_sql(dialect: str) -> str:
    is_pg = dialect == "postgresql"
    affected = ops_for(dialect).id_set_sql("pks")
    if is_pg:
        models = (
            "SELECT run_pk, jsonb_agg(DISTINCT model ORDER BY model) AS models "
            "FROM llm_calls WHERE run_pk IN (SELECT v FROM affected) GROUP BY run_pk"
        )
        empty_json, genai = "'[]'::jsonb", "calc.is_genai = 1"
    else:
        models = (
            "SELECT run_pk, json_group_array(model) AS models FROM (SELECT DISTINCT run_pk, model "
            "FROM llm_calls WHERE run_pk IN (SELECT v FROM affected) ORDER BY run_pk, model) "
            "GROUP BY run_pk"
        )
        empty_json, genai = "'[]'", "calc.is_genai"
    return f"""
WITH affected AS ({affected}),
s AS (SELECT sp.pk, sp.run_pk, sp.span_id, sp.parent_span_id, sp.name, sp.category, sp.start_ns,
             sp.end_ns, sp.status_code, sp.is_genai, sp.size_bytes, sp.source_pk, sp.agent_name,
             sp.session_ext, sp.agent_usage_input, sp.agent_usage_output
      FROM spans sp WHERE sp.run_pk IN (SELECT v FROM affected)),
root AS (SELECT * FROM (SELECT s.*, {_FIRST} AS rn FROM s WHERE parent_span_id IS NULL) x
         WHERE rn = 1),
first_any AS (SELECT * FROM (SELECT run_pk, source_pk, {_FIRST} AS rn FROM s) x WHERE rn = 1),
first_sess AS (SELECT * FROM (SELECT run_pk, session_ext, {_FIRST} AS rn FROM s
               WHERE session_ext IS NOT NULL) x WHERE rn = 1),
first_agent AS (SELECT * FROM (SELECT run_pk, agent_name, {_FIRST} AS rn FROM s
                WHERE category = 'agent' AND agent_name IS NOT NULL) x WHERE rn = 1),
agent_usage AS (SELECT * FROM (SELECT run_pk, agent_usage_input, agent_usage_output,
                {_FIRST} AS rn FROM s WHERE category = 'agent'
                AND (agent_usage_input IS NOT NULL OR agent_usage_output IS NOT NULL)) x
                WHERE rn = 1),
agg AS (SELECT run_pk, min(start_ns) AS start_ns, max(end_ns) AS last_span_ns,
               count(*) AS span_count,
               sum(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS error_spans,
               max(CASE WHEN is_genai THEN 1 ELSE 0 END) AS is_genai,
               sum(size_bytes) AS bytes_est, count(DISTINCT source_pk) AS source_count
        FROM s GROUP BY run_pk),
orphans AS (SELECT c.run_pk, count(*) AS n FROM s c WHERE c.parent_span_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM s p WHERE p.run_pk = c.run_pk
                            AND p.span_id = c.parent_span_id) GROUP BY c.run_pk),
llm AS (SELECT run_pk, count(*) AS n, sum(coalesce(input_tokens, 0)) AS it,
               sum(coalesce(output_tokens, 0)) AS ot,
               sum(coalesce(cached_input_tokens, 0)) AS ct,
               sum(coalesce(cache_write_tokens, 0)) AS cwt,
               sum(coalesce(reasoning_tokens, 0)) AS rt, sum(cost_nusd) AS cost,
               sum(CASE WHEN unpriced THEN 1 ELSE 0 END) AS unpriced_calls
        FROM llm_calls WHERE run_pk IN (SELECT v FROM affected) GROUP BY run_pk),
models AS ({models}),
tools AS (SELECT run_pk, count(*) AS n FROM tool_calls
          WHERE run_pk IN (SELECT v FROM affected) GROUP BY run_pk),
calc AS (
  SELECT agg.run_pk, agg.start_ns, agg.last_span_ns, agg.span_count, agg.error_spans,
         agg.is_genai, agg.bytes_est, agg.source_count,
         root.pk AS root_pk, root.name AS root_name, root.end_ns AS root_end,
         root.status_code AS root_status,
         coalesce(root.agent_name, first_agent.agent_name) AS agent_name,
         coalesce(root.source_pk, first_any.source_pk) AS source_pk,
         coalesce(root.session_ext, first_sess.session_ext) AS session_ext,
         coalesce(orphans.n, 0) AS missing_parents,
         coalesce(llm.n, 0) AS llm_n, llm.it, llm.ot, llm.ct, llm.cwt, llm.rt, llm.cost,
         coalesce(llm.unpriced_calls, 0) AS unpriced_calls, models.models,
         coalesce(tools.n, 0) AS tool_n,
         agent_usage.agent_usage_input AS aui, agent_usage.agent_usage_output AS auo
  FROM agg LEFT JOIN root ON root.run_pk = agg.run_pk
  LEFT JOIN first_any ON first_any.run_pk = agg.run_pk
  LEFT JOIN first_sess ON first_sess.run_pk = agg.run_pk
  LEFT JOIN first_agent ON first_agent.run_pk = agg.run_pk
  LEFT JOIN agent_usage ON agent_usage.run_pk = agg.run_pk
  LEFT JOIN orphans ON orphans.run_pk = agg.run_pk
  LEFT JOIN llm ON llm.run_pk = agg.run_pk
  LEFT JOIN models ON models.run_pk = agg.run_pk
  LEFT JOIN tools ON tools.run_pk = agg.run_pk)
UPDATE runs SET
  start_ns = calc.start_ns, last_span_ns = calc.last_span_ns, span_count = calc.span_count,
  error_spans = calc.error_spans, is_genai = ({genai}), bytes_est = calc.bytes_est,
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
  models = coalesce(calc.models, {empty_json}),
  session_pk = (SELECT ss.pk FROM sessions ss WHERE ss.project_pk = runs.project_pk
                AND ss.external_id = calc.session_ext),
  updated_at_ns = :now_ns
FROM calc WHERE runs.pk = calc.run_pk
"""  # noqa: S608 - trusted fragments only; every value is bound


def _sessions_sql(dialect: str) -> str:
    affected = ops_for(dialect).id_set_sql("pks")
    unpriced = "(x.unpriced = 1)" if dialect == "postgresql" else "x.unpriced"
    return f"""
WITH aff AS ({affected}),
x AS (SELECT aff.v AS pk, count(r.pk) AS n, min(r.start_ns) AS first_ns,
             max(coalesce(r.end_ns, r.last_span_ns)) AS last_ns,
             coalesce(sum(r.input_tokens), 0) AS it, coalesce(sum(r.output_tokens), 0) AS ot,
             coalesce(sum(r.cached_input_tokens), 0) AS ct,
             coalesce(sum(r.cost_nusd), 0) AS cost,
             coalesce(max(CASE WHEN r.unpriced_calls > 0 THEN 1 ELSE 0 END), 0) AS unpriced
      FROM aff LEFT JOIN runs r ON r.session_pk = aff.v GROUP BY aff.v)
UPDATE sessions SET run_count = x.n, first_ns = coalesce(x.first_ns, sessions.first_ns),
  last_ns = coalesce(x.last_ns, sessions.last_ns), input_tokens = x.it, output_tokens = x.ot,
  cached_input_tokens = x.ct, cost_nusd = x.cost, includes_unpriced = {unpriced},
  updated_at_ns = :now_ns
FROM x WHERE sessions.pk = x.pk
"""  # noqa: S608 - trusted fragments only; every value is bound


def _session_pks(conn: Connection, run_pks: Sequence[int]) -> set[int]:
    rows = conn.execute(
        select(s.runs.c.session_pk).where(s.runs.c.pk.in_(run_pks)).distinct()
    ).scalars()
    return {pk for pk in rows if pk is not None}


def recompute_sessions(conn: Connection, session_pks: Sequence[int], now_ns: int) -> None:
    """Recompute Session aggregates from their Runs (REQ-036.b), locking them first on PG."""
    if not session_pks:
        return
    ops = ops_for(conn.dialect.name)
    pks = sorted(session_pks)
    if conn.dialect.name == "postgresql":  # DD-004: Sessions are locked after Runs, by pk
        conn.execute(
            select(s.sessions.c.pk)
            .where(s.sessions.c.pk.in_(pks))
            .order_by(s.sessions.c.pk)
            .with_for_update()
        ).all()
    conn.execute(
        text(_sessions_sql(conn.dialect.name)), {"pks": ops.id_set_value(pks), "now_ns": now_ns}
    )


def recompute_runs(
    conn: Connection, run_pks: Sequence[int], *, now_ns: int, window_ns: int
) -> None:
    """Recompute ``run_pks`` (already locked by the caller) and every Session they touch."""
    if not run_pks:
        return
    ops = ops_for(conn.dialect.name)
    before = _session_pks(conn, run_pks)
    conn.execute(
        text(_runs_sql(conn.dialect.name)),
        {"pks": ops.id_set_value(run_pks), "now_ns": now_ns, "cutoff_ns": now_ns - window_ns},
    )
    recompute_sessions(conn, sorted(before | _session_pks(conn, run_pks)), now_ns)
