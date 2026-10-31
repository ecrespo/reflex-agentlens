"""The write path shared by the SQL backends (API §5.1; Data Model Q6, Q7; DD-003, DD-004).

:func:`write_batch` runs on a sync connection inside the caller's transaction (async backends
call it through ``run_sync``), so one implementation serves SQLite and PostgreSQL. Steps:

1. Upsert Sources, Resources and Sessions by natural key (REQ-031, REQ-036.a).
2. Create or row-lock the Run stubs in one ``INSERT … ON CONFLICT DO UPDATE … RETURNING``,
   rows sorted by (``project_pk``, ``trace_id``) — the DD-004 lock order (Analyze A-01).
3. Insert spans with ``ON CONFLICT DO NOTHING RETURNING``; duplicates (REQ-038) are the spans
   not returned, and get no derived rows.
4. Insert derived rows and search documents for the new spans only (REQ-073.a).
5. Recompute the affected Runs and their Sessions from scratch (Q7, REQ-037.a).

Sessions are created for every session identifier seen, even when the Run ends up attached to
another one (REQ-036.c): which identifier wins depends on spans that may arrive later, so
creating only the winner would make the Sessions table depend on arrival order. Sessions left
with no Runs are hidden by the query layer and deleted by the purge job.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TypeVar

from sqlalchemy import Connection, Table, select

from reflex_agentlens.core.errors import NotFound
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.search import search_normalize, truncate_utf8
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.dialect import SqlDialectOps, ops_for
from reflex_agentlens.store.recompute import recompute_runs
from reflex_agentlens.store.records import NormalizedBatch, NormalizedSpan, RunKey, SourceKey

__all__ = ["MAX_BIND_PARAMS", "WriteOutcome", "write_batch"]

MAX_BIND_PARAMS = 32_000  # SQLite's limit is 32,766; PostgreSQL's 65,535
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    inserted: int
    duplicates: int
    affected_runs: list[RunKey]


def _chunks(rows: Sequence[T], columns: int) -> Iterator[Sequence[T]]:
    size = max(1, MAX_BIND_PARAMS // max(1, columns))
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _project_pk(conn: Connection, project_id: str) -> int:
    pk = conn.execute(select(s.projects.c.pk).where(s.projects.c.id == project_id)).scalar()
    if pk is None:
        raise NotFound("project")
    return int(pk)


def _dedupe(spans: Iterable[NormalizedSpan]) -> list[NormalizedSpan]:
    """Keep the first occurrence of each (trace_id, span_id) (REQ-038.a)."""
    seen: dict[tuple[bytes, bytes], NormalizedSpan] = {}
    for span in spans:
        seen.setdefault((span.trace_id, span.span_id), span)
    return list(seen.values())


def _upsert_sources(
    conn: Connection, ops: SqlDialectOps, project_pk: int, spans: list[NormalizedSpan]
) -> dict[SourceKey, int]:
    bounds: dict[SourceKey, tuple[int, int]] = {}
    for sp in spans:
        lo, hi = bounds.get(sp.source, (sp.start_ns, sp.end_ns))
        bounds[sp.source] = (min(lo, sp.start_ns), max(hi, sp.end_ns))
    rows = [
        {
            "project_pk": project_pk,
            "service_namespace": key.service_namespace,
            "service_name": key.service_name,
            "first_seen_ns": lo,
            "last_seen_ns": hi,
        }
        for key, (lo, hi) in sorted(
            bounds.items(), key=lambda kv: (kv[0].service_namespace, kv[0].service_name)
        )
    ]
    t = s.sources
    out: dict[SourceKey, int] = {}
    for chunk in _chunks(rows, 5):
        stmt = ops.insert(t).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[t.c.project_pk, t.c.service_namespace, t.c.service_name],
            set_={
                "first_seen_ns": ops.least(t.c.first_seen_ns, stmt.excluded.first_seen_ns),
                "last_seen_ns": ops.greatest(t.c.last_seen_ns, stmt.excluded.last_seen_ns),
            },
        ).returning(t.c.pk, t.c.service_namespace, t.c.service_name)
        for pk, namespace, name in conn.execute(stmt):
            out[SourceKey(service_name=name, service_namespace=namespace)] = pk
    return out


def _upsert_resources(
    conn: Connection, ops: SqlDialectOps, project_pk: int, spans: list[NormalizedSpan]
) -> dict[bytes, int]:
    by_hash = {sp.resource.attrs_hash: sp.resource for sp in reversed(spans)}  # first wins
    rows = [
        {
            "project_pk": project_pk,
            "attrs_hash": h,
            "attributes": r.attributes,
            "schema_url": r.schema_url,
        }
        for h, r in sorted(by_hash.items())
    ]
    t = s.resources
    for chunk in _chunks(rows, 4):
        conn.execute(ops.insert(t).values(list(chunk)).on_conflict_do_nothing())
    out: dict[bytes, int] = {}
    hashes = sorted(by_hash)
    for chunk_h in _chunks(hashes, 1):
        rows_ = conn.execute(
            select(t.c.attrs_hash, t.c.pk).where(
                t.c.project_pk == project_pk, t.c.attrs_hash.in_(list(chunk_h))
            )
        )
        out.update({bytes(h): pk for h, pk in rows_})
    return out


def _upsert_sessions(
    conn: Connection, ops: SqlDialectOps, project_pk: int, spans: list[NormalizedSpan], now_ns: int
) -> None:
    bounds: dict[str, tuple[int, int]] = {}
    for sp in spans:
        if sp.session is None:
            continue
        lo, hi = bounds.get(sp.session.external_id, (sp.start_ns, sp.end_ns))
        bounds[sp.session.external_id] = (min(lo, sp.start_ns), max(hi, sp.end_ns))
    if not bounds:
        return
    rows = [
        {
            "id": new_ulid(),
            "project_pk": project_pk,
            "external_id": ext,
            "first_ns": lo,
            "last_ns": hi,
            "updated_at_ns": now_ns,
        }
        for ext, (lo, hi) in sorted(bounds.items())
    ]
    t = s.sessions
    for chunk in _chunks(rows, 6):
        # DO NOTHING: aggregates (first/last included) come from the recompute (REQ-036.b).
        conn.execute(ops.insert(t).values(list(chunk)).on_conflict_do_nothing())


def _lock_run_stubs(
    conn: Connection, ops: SqlDialectOps, project_pk: int, spans: list[NormalizedSpan], now_ns: int
) -> dict[bytes, int]:
    bounds: dict[bytes, tuple[int, int]] = {}
    for sp in spans:
        lo, hi = bounds.get(sp.trace_id, (sp.start_ns, sp.end_ns))
        bounds[sp.trace_id] = (min(lo, sp.start_ns), max(hi, sp.end_ns))
    rows = [
        {
            "project_pk": project_pk,
            "trace_id": trace_id,
            "start_ns": lo,
            "last_span_ns": hi,
            "status": "in_progress",
            "updated_at_ns": now_ns,
        }
        for trace_id, (lo, hi) in sorted(bounds.items())  # DD-004 lock order
    ]
    t = s.runs
    out: dict[bytes, int] = {}
    for chunk in _chunks(rows, 6):
        stmt = ops.insert(t).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[t.c.project_pk, t.c.trace_id],
            set_={"updated_at_ns": stmt.excluded.updated_at_ns},  # DO UPDATE takes the row lock
        ).returning(t.c.pk, t.c.trace_id)
        out.update({bytes(trace_id): pk for pk, trace_id in conn.execute(stmt)})
    return out


def _span_row(
    sp: NormalizedSpan, project_pk: int, run_pk: int, source_pk: int, resource_pk: int
) -> dict[str, Any]:
    return {
        "project_pk": project_pk,
        "run_pk": run_pk,
        "span_id": sp.span_id,
        "parent_span_id": sp.parent_span_id,
        "source_pk": source_pk,
        "resource_pk": resource_pk,
        "name": sp.name,
        "otel_kind": sp.otel_kind,
        "category": sp.category,
        "is_genai": sp.is_genai,
        "start_ns": sp.start_ns,
        "end_ns": sp.end_ns,
        "status_code": sp.status_code,
        "status_message": sp.status_message,
        "scope_name": sp.scope_name,
        "scope_version": sp.scope_version,
        "attributes": sp.attributes,
        "events": sp.events,
        "links": sp.links,
        "dropped_counts": sp.dropped_counts,
        "dropped_keys": sp.dropped_keys,
        "agent_name": sp.agent_name,
        "session_ext": sp.session.external_id if sp.session else None,
        "agent_usage_input": sp.agent_usage_input,
        "agent_usage_output": sp.agent_usage_output,
        "flags": sp.flags,
        "mapping_version": sp.mapping_version,
        "size_bytes": sp.size_bytes,
        "received_at_ns": sp.received_at_ns,
    }


def _insert_many(conn: Connection, table: Table, rows: list[dict[str, Any]]) -> None:
    if rows:
        conn.execute(table.insert(), rows)


def write_batch(
    conn: Connection, batch: NormalizedBatch, *, now_ns: int, window_ns: int
) -> WriteOutcome:
    """Persist ``batch`` in the caller's transaction; see the module docstring."""
    ops = ops_for(conn.dialect.name)
    project_pk = _project_pk(conn, batch.project_id)
    spans = _dedupe(batch.spans)
    if not spans:
        return WriteOutcome(0, 0, [])

    source_pks = _upsert_sources(conn, ops, project_pk, spans)
    resource_pks = _upsert_resources(conn, ops, project_pk, spans)
    _upsert_sessions(conn, ops, project_pk, spans, now_ns)
    run_pks = _lock_run_stubs(conn, ops, project_pk, spans, now_ns)

    by_key = {(run_pks[sp.trace_id], sp.span_id): sp for sp in spans}
    rows = [
        _span_row(sp, project_pk, rp, source_pks[sp.source], resource_pks[sp.resource.attrs_hash])
        for (rp, _), sp in by_key.items()
    ]
    t = s.spans
    returned: list[tuple[int, int, bytes]] = []
    for chunk in _chunks(rows, len(rows[0])):
        stmt = (
            ops.insert(t)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=[t.c.run_pk, t.c.span_id])
            .returning(t.c.pk, t.c.run_pk, t.c.span_id)
        )
        returned.extend((pk, rp, bytes(sid)) for pk, rp, sid in conn.execute(stmt))

    llm, tool, appr, node, docs = [], [], [], [], []
    for pk, rp, sid in returned:
        sp = by_key[(rp, sid)]
        keys = {"span_pk": pk, "run_pk": rp, "project_pk": project_pk}
        if sp.llm_call is not None:
            llm.append(keys | asdict(sp.llm_call))
        if sp.tool_call is not None:
            tool.append(keys | asdict(sp.tool_call))
        if sp.approval is not None:
            appr.append(keys | asdict(sp.approval))
        if sp.graph_node is not None:
            node.append(keys | asdict(sp.graph_node))
        if sp.search_doc:
            doc = search_normalize(truncate_utf8(sp.search_doc))
            docs.append({"pk": pk, "run_pk": rp, "project_pk": project_pk, "doc": doc})
    _insert_many(conn, s.llm_calls, llm)
    _insert_many(conn, s.tool_calls, tool)
    _insert_many(conn, s.approvals, appr)
    _insert_many(conn, s.graph_nodes, node)
    if docs:
        conn.execute(ops.search_insert(), docs)

    affected = sorted({rp for _, rp, _ in returned})
    recompute_runs(conn, affected, now_ns=now_ns, window_ns=window_ns)
    pk_to_trace = {pk: trace for trace, pk in run_pks.items()}
    return WriteOutcome(
        inserted=len(returned),
        duplicates=len(batch.spans) - len(returned),
        affected_runs=sorted(RunKey(batch.project_id, pk_to_trace[pk]) for pk in affected),
    )
