"""Order-independent snapshot of one Project's derived views, keyed by natural keys.

Surrogate keys (``pk``), public IDs minted at insert time (Session ULIDs) and audit columns
(``updated_at_ns``) depend on arrival order by design; everything else must not (REQ-037.a).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, select

from reflex_agentlens.store import schema as s

RUN_SKIP = {"pk", "project_pk", "source_pk", "session_pk", "root_span_pk", "updated_at_ns"}
SESSION_SKIP = {"pk", "id", "project_pk", "updated_at_ns"}
DERIVED_SKIP = {"span_pk", "run_pk", "project_pk"}


def _rows(conn: Connection, stmt: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in conn.execute(stmt)]


def snapshot(conn: Connection, project_id: str) -> dict[str, Any]:
    p = select(s.projects.c.pk).where(s.projects.c.id == project_id).scalar_subquery()
    r, src, ses, sp = s.runs, s.sources, s.sessions, s.spans
    root = sp.alias("root")
    runs = _rows(
        conn,
        select(
            *[c for c in r.columns if c.name not in RUN_SKIP],
            src.c.service_namespace.label("source_ns"),
            src.c.service_name.label("source_name"),
            ses.c.external_id.label("session_ext"),
            root.c.span_id.label("root_span_id"),
        )
        .select_from(
            r.outerjoin(src, src.c.pk == r.c.source_pk)
            .outerjoin(ses, ses.c.pk == r.c.session_pk)
            .outerjoin(root, root.c.pk == r.c.root_span_pk)
        )
        .where(r.c.project_pk == p)
        .order_by(r.c.trace_id),
    )
    sessions = _rows(
        conn,
        select(*[c for c in ses.columns if c.name not in SESSION_SKIP])
        .where(ses.c.project_pk == p, ses.c.run_count > 0)
        .order_by(ses.c.external_id),
    )
    all_sessions = (
        conn.execute(
            select(ses.c.external_id).where(ses.c.project_pk == p).order_by(ses.c.external_id)
        )
        .scalars()
        .all()
    )
    derived: dict[str, list[dict[str, Any]]] = {}
    for table in (s.llm_calls, s.tool_calls, s.approvals, s.graph_nodes):
        derived[table.name] = _rows(
            conn,
            select(*[c for c in table.columns if c.name not in DERIVED_SKIP], sp.c.span_id)
            .join(sp, sp.c.pk == table.c.span_pk)
            .where(table.c.project_pk == p)
            .order_by(sp.c.span_id),
        )
    spans = conn.execute(
        select(sp.c.span_id, sp.c.name).where(sp.c.project_pk == p).order_by(sp.c.span_id)
    ).all()
    sources = conn.execute(
        select(src.c.service_namespace, src.c.service_name, src.c.first_seen_ns, src.c.last_seen_ns)
        .where(src.c.project_pk == p)
        .order_by(src.c.service_namespace, src.c.service_name)
    ).all()
    return {
        "runs": runs,
        "sessions": sessions,
        "session_ids": list(all_sessions),
        "spans": [tuple(x) for x in spans],
        "sources": [tuple(x) for x in sources],
        **derived,
    }
