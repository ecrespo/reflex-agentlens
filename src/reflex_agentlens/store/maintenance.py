"""Derived-view maintenance: finalizer, rebuild, cost recompute (plan task P2-08.3).

REQ-032.b (stale ``in_progress`` → ``incomplete``), REQ-042 (rebuild), REQ-062 (recompute
costs), DD-004 (lock order).

Every function here does **one bounded batch in the caller's transaction** and returns where it
stopped; the backend loops, one transaction per batch, so ingestion keeps going between batches
(REQ-042.b, REQ-092.c). Each batch locks its Runs first, ordered by (``project_pk``,
``trace_id``) as the write path does (DD-004): ``FOR UPDATE`` on PostgreSQL (``SKIP LOCKED`` for
the finalizer, which may simply retry later), the single writer connection on SQLite.

Because Run and Session rows are a pure function of the stored spans and derived rows (Q7), a
rebuild that interleaves with ingestion converges to the same result as one with ingestion
stopped: whichever transaction commits last recomputes the Run from the full stored set.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import fields
from typing import Any, TypeVar

from sqlalchemy import Connection, bindparam, delete, func, literal, select, text, tuple_, update

from reflex_agentlens.core.pricing import llm_cost, match_entry
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.dialect import ops_for
from reflex_agentlens.store.recompute import recompute_runs
from reflex_agentlens.store.records import (
    ApprovalRow,
    GraphNodeRow,
    LLMCallRow,
    NormalizedSpan,
    PricingVersion,
    RebuildReport,
    RebuildScope,
    ResourceIn,
    RunKey,
    SessionKey,
    SourceKey,
    SpanDeriver,
    TimeRange,
    ToolCallRow,
)
from reflex_agentlens.store.writepath import insert_derived, upsert_sessions

__all__ = [
    "RunCursor",
    "finalize_stale_runs",
    "rebuild_batch",
    "recompute_costs_batch",
    "update_mapping_version_min",
]

RunCursor = tuple[int, bytes]  # (project_pk, trace_id) of the last Run of a batch
r, sp, p = s.runs, s.spans, s.projects
D = TypeVar("D")
_DERIVED: tuple[tuple[Any, type[Any], str], ...] = (
    (s.llm_calls, LLMCallRow, "llm_call"),
    (s.tool_calls, ToolCallRow, "tool_call"),
    (s.approvals, ApprovalRow, "approval"),
    (s.graph_nodes, GraphNodeRow, "graph_node"),
)
_SPAN_DERIVED_COLS = (
    "category", "is_genai", "agent_name", "session_ext", "agent_usage_input",
    "agent_usage_output", "flags", "mapping_version",
)  # fmt: skip


def _lock_runs(conn: Connection, stmt: Any, *, skip_locked: bool = False) -> list[Any]:
    stmt = stmt.order_by(r.c.project_pk, r.c.trace_id)
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update(of=r, skip_locked=skip_locked)
    return list(conn.execute(stmt).all())


def _run_state(conn: Connection, run_pks: Sequence[int]) -> dict[int, tuple[Any, ...]]:
    cols = [c for c in r.columns if c.name != "updated_at_ns"]
    rows = conn.execute(select(*cols).where(r.c.pk.in_(run_pks))).all()
    return {row[0]: tuple(row) for row in rows}


# ── Q12: finalizer (REQ-032.b) ───────────────────────────────────────────────────────────────


def finalize_stale_runs(
    conn: Connection, older_than_ns: int, limit: int, now_ns: int
) -> list[RunKey]:
    """Recompute up to ``limit`` ``in_progress`` Runs silent since before ``older_than_ns``;
    they become ``incomplete`` unless spans arrived meanwhile. Returns the Runs that changed."""
    rows = _lock_runs(
        conn,
        select(r.c.pk, p.c.id, r.c.trace_id)
        .select_from(r.join(p, p.c.pk == r.c.project_pk))
        .where(r.c.status == "in_progress", r.c.last_span_ns < older_than_ns)
        .limit(limit),
        skip_locked=True,
    )
    if not rows:
        return []
    pks = [row[0] for row in rows]
    # cutoff = now - window = older_than_ns: the finalization rule of Q7 with this threshold
    recompute_runs(conn, pks, now_ns=now_ns, window_ns=now_ns - older_than_ns)
    flipped = set(
        conn.execute(select(r.c.pk).where(r.c.pk.in_(pks), r.c.status != "in_progress")).scalars()
    )
    return sorted(RunKey(pid, bytes(trace)) for pk, pid, trace in rows if pk in flipped)


# ── rebuild (REQ-042) ────────────────────────────────────────────────────────────────────────


def _as(cls: type[D], m: Any) -> D:
    values = {f.name: m[f.name] for f in fields(cls)}  # type: ignore[arg-type]
    for name in ("unpriced", "content_dropped"):
        if name in values:
            values[name] = bool(values[name])
    return cls(**values)


def _stored_spans(
    conn: Connection, run_pks: Sequence[int]
) -> list[tuple[int, int, int, NormalizedSpan]]:
    """Spans of ``run_pks`` as stored, as ``(span_pk, run_pk, project_pk, span)``."""
    derived: dict[str, dict[int, Any]] = {}
    for table, cls, attr in _DERIVED:
        rows = conn.execute(select(table).where(table.c.run_pk.in_(run_pks))).mappings()
        derived[attr] = {m["span_pk"]: _as(cls, m) for m in rows}
    src, res = s.sources, s.resources
    rows = conn.execute(
        select(sp, r.c.trace_id, src.c.service_name, src.c.service_namespace,
               res.c.attrs_hash, res.c.attributes.label("res_attrs"), res.c.schema_url)
        .select_from(sp.join(r, r.c.pk == sp.c.run_pk)
                     .join(src, src.c.pk == sp.c.source_pk)
                     .join(res, res.c.pk == sp.c.resource_pk))
        .where(sp.c.run_pk.in_(run_pks))
        .order_by(sp.c.pk)
    ).mappings()  # fmt: skip
    out = []
    for m in rows:
        span = NormalizedSpan(
            trace_id=bytes(m["trace_id"]),
            span_id=bytes(m["span_id"]),
            parent_span_id=None if m["parent_span_id"] is None else bytes(m["parent_span_id"]),
            source=SourceKey(m["service_name"], m["service_namespace"]),
            resource=ResourceIn(bytes(m["attrs_hash"]), m["res_attrs"], m["schema_url"]),
            name=m["name"],
            start_ns=m["start_ns"],
            end_ns=m["end_ns"],
            mapping_version=m["mapping_version"],
            size_bytes=m["size_bytes"],
            received_at_ns=m["received_at_ns"],
            otel_kind=m["otel_kind"],
            category=m["category"],
            is_genai=bool(m["is_genai"]),
            status_code=m["status_code"],
            status_message=m["status_message"],
            scope_name=m["scope_name"],
            scope_version=m["scope_version"],
            attributes=m["attributes"],
            events=m["events"],
            links=m["links"],
            dropped_counts=m["dropped_counts"],
            dropped_keys=m["dropped_keys"],
            agent_name=m["agent_name"],
            session=SessionKey(m["session_ext"]) if m["session_ext"] is not None else None,
            agent_usage_input=m["agent_usage_input"],
            agent_usage_output=m["agent_usage_output"],
            flags=m["flags"],
            **{attr: derived[attr].get(m["pk"]) for _, _, attr in _DERIVED},
        )
        out.append((m["pk"], m["run_pk"], m["project_pk"], span))
    return out


def _delete_derived(conn: Connection, run_pks: Sequence[int]) -> None:
    ops = ops_for(conn.dialect.name)
    for table, _, _ in _DERIVED:
        conn.execute(delete(table).where(table.c.run_pk.in_(run_pks)))
    ids = ops.id_set_sql("pks")
    if conn.dialect.name == "sqlite":
        sql = (
            f"DELETE FROM span_search WHERE rowid IN (SELECT pk FROM spans WHERE run_pk IN ({ids}))"  # noqa: S608
        )
    else:
        sql = f"DELETE FROM span_search WHERE run_pk IN ({ids})"  # noqa: S608
    conn.execute(text(sql), {"pks": ops.id_set_value(run_pks)})


def rebuild_batch(
    conn: Connection,
    scope: RebuildScope,
    deriver: SpanDeriver,
    pricing: PricingVersion,
    after: RunCursor | None,
    batch_runs: int,
    now_ns: int,
    window_ns: int,
) -> tuple[RebuildReport, RunCursor | None]:
    """Re-derive every span of the next ``batch_runs`` Runs in scope, then recompute them."""
    stmt = select(r.c.pk, r.c.project_pk, r.c.trace_id).limit(batch_runs)
    if scope.project_ids is not None:
        stmt = stmt.where(
            r.c.project_pk.in_(select(p.c.pk).where(p.c.id.in_(sorted(scope.project_ids))))
        )
    if scope.since_ns is not None:
        stmt = stmt.where(r.c.last_span_ns >= scope.since_ns)
    if after is not None:
        stmt = stmt.where(
            tuple_(r.c.project_pk, r.c.trace_id) > tuple_(literal(after[0]), literal(after[1]))
        )
    runs = _lock_runs(conn, stmt)
    if not runs:
        return RebuildReport(), None
    pks = [row[0] for row in runs]
    before = _run_state(conn, pks)
    stored = _stored_spans(conn, pks)
    fresh = [(pk, rp, pj, deriver(span, pricing)) for pk, rp, pj, span in stored]

    if fresh:
        params = [
            {"b_pk": pk, **{f"b_{c}": _span_value(span, c) for c in _SPAN_DERIVED_COLS}}
            for pk, _, _, span in fresh
        ]
        conn.execute(
            update(sp).where(sp.c.pk == bindparam("b_pk"))
            .values({c: bindparam(f"b_{c}") for c in _SPAN_DERIVED_COLS}),
            params,
        )  # fmt: skip
    _delete_derived(conn, pks)
    ops = ops_for(conn.dialect.name)
    by_project: dict[int, list[NormalizedSpan]] = defaultdict(list)
    for _, _, pj, span in fresh:
        by_project[pj].append(span)
    for pj, spans in sorted(by_project.items()):
        upsert_sessions(conn, ops, pj, spans, now_ns)
    insert_derived(conn, ops, fresh)
    recompute_runs(conn, pks, now_ns=now_ns, window_ns=window_ns)
    after_state = _run_state(conn, pks)
    changed = sum(before[pk] != after_state[pk] for pk in pks)
    last = runs[-1]
    return RebuildReport(len(pks), len(fresh), changed), (last[1], bytes(last[2]))


def _span_value(span: NormalizedSpan, column: str) -> Any:
    if column == "session_ext":
        return span.session.external_id if span.session else None
    return getattr(span, column)


def update_mapping_version_min(conn: Connection) -> int | None:
    """``app_meta.mapping_version_min`` = lowest ``MAPPING_VERSION`` stored (Data Model §2.1)."""
    low = conn.execute(select(func.min(sp.c.mapping_version))).scalar()
    ops = ops_for(conn.dialect.name)
    t = s.app_meta
    if low is None:
        conn.execute(delete(t).where(t.c.key == "mapping_version_min"))
        return None
    stmt = ops.insert(t).values(key="mapping_version_min", value=str(low))
    conn.execute(stmt.on_conflict_do_update(index_elements=[t.c.key], set_={"value": str(low)}))
    return int(low)


# ── cost recompute (REQ-062) ─────────────────────────────────────────────────────────────────


def recompute_costs_batch(
    conn: Connection,
    project_pk: int,
    time_range: TimeRange,
    pricing: PricingVersion,
    after: RunCursor | None,
    batch_runs: int,
    now_ns: int,
    window_ns: int,
) -> tuple[int, RunCursor | None]:
    """Re-price the LLM calls started in ``time_range`` of the next ``batch_runs`` Runs with
    ``pricing`` (its version recorded on each call), then recompute their Runs and Sessions."""
    lc = s.llm_calls
    in_range = (lc.c.start_ns >= time_range.start_ns, lc.c.start_ns < time_range.end_ns)
    stmt = (
        select(r.c.pk, r.c.project_pk, r.c.trace_id)
        .where(r.c.project_pk == project_pk,
               r.c.pk.in_(select(lc.c.run_pk).where(lc.c.project_pk == project_pk, *in_range)))
        .limit(batch_runs)
    )  # fmt: skip
    if after is not None:
        stmt = stmt.where(r.c.trace_id > after[1])
    runs = _lock_runs(conn, stmt)
    if not runs:
        return 0, None
    pks = [row[0] for row in runs]
    calls = conn.execute(
        select(lc.c.span_pk, lc.c.provider, lc.c.request_model, lc.c.response_model,
               lc.c.input_tokens, lc.c.output_tokens, lc.c.cached_input_tokens,
               lc.c.cache_write_tokens)
        .where(lc.c.run_pk.in_(pks), *in_range)
    ).all()  # fmt: skip
    params = []
    for span_pk, provider, req, resp, it, ot, ct, cw in calls:
        entry = match_entry(pricing.entries, provider, resp, req)
        cost = llm_cost(entry, it, ot, ct, cw)
        params.append(
            {"b_pk": span_pk, "b_cost": cost, "b_unpriced": cost is None,
             "b_version": pricing.meta.version}
        )  # fmt: skip
    if params:
        conn.execute(
            update(lc).where(lc.c.span_pk == bindparam("b_pk")).values(
                cost_nusd=bindparam("b_cost"), unpriced=bindparam("b_unpriced"),
                pricing_version=bindparam("b_version"),
            ),
            params,
        )  # fmt: skip
    recompute_runs(conn, pks, now_ns=now_ns, window_ns=window_ns)
    last = runs[-1]
    return len(params), (last[1], bytes(last[2]))
