"""Session reads (plan task P2-07.2; Data Model Q9; REQ-105).

A Session left with no Run (``run_count = 0``, e.g. after a purge, or created for a conversation
ID whose Runs went elsewhere) is never returned; the purge job deletes such rows (Data Model §2.8).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, Connection, and_, exists, func, literal, or_, select, tuple_

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.reads import _bytewise, _scope_pks, _summary, _summary_select
from reflex_agentlens.store.records import (
    MAX_PAGE_SIZE,
    Page,
    ProjectScope,
    SessionCursor,
    SessionDetail,
    SessionQuery,
    SessionRun,
    SessionSummary,
)

__all__ = ["get_session", "list_sessions"]

ses, p, r, src = s.sessions, s.projects, s.runs, s.sources
MAX_SORT = 2**62


def _summary_row(m: Any) -> SessionSummary:
    return SessionSummary(
        project_id=m["project_id"],
        session_id=m["id"],
        external_id=m["external_id"],
        first_ns=m["first_ns"],
        last_ns=m["last_ns"],
        run_count=m["run_count"],
        input_tokens=m["input_tokens"],
        output_tokens=m["output_tokens"],
        cached_input_tokens=m["cached_input_tokens"],
        cost_nusd=m["cost_nusd"],
        includes_unpriced=bool(m["includes_unpriced"]),
    )


def _sort_key(sort: str) -> tuple[ColumnElement[Any], bool]:
    field, direction = sort.rsplit("_", 1)
    column: ColumnElement[Any] = {
        "last": ses.c.last_ns,
        "runs": ses.c.run_count,
        "tokens": ses.c.input_tokens + ses.c.output_tokens,
        "cost": ses.c.cost_nusd,
    }[field]
    return column, direction == "desc"


def list_sessions(conn: Connection, q: SessionQuery) -> Page[SessionSummary, SessionCursor]:
    """Q9a: Sessions by Project, Source and time range, four sort keys, keyset (REQ-105.a)."""
    if not 1 <= q.limit <= MAX_PAGE_SIZE:
        raise ValidationError("limit", f"must be between 1 and {MAX_PAGE_SIZE}")
    if not q.project_ids:
        return Page([], None)
    where: list[ColumnElement[bool]] = [
        ses.c.project_pk.in_(_scope_pks(q.project_ids)),
        ses.c.run_count > 0,
    ]
    if q.time_range is not None:  # overlap with [start, end)
        where += [ses.c.last_ns >= q.time_range.start_ns, ses.c.first_ns < q.time_range.end_ns]
    if q.sources:
        where.append(
            exists(
                select(literal(1))
                .select_from(r.join(src, src.c.pk == r.c.source_pk))
                .where(
                    r.c.session_pk == ses.c.pk,
                    or_(*[
                        and_(src.c.service_name == k.service_name,
                             src.c.service_namespace == k.service_namespace)
                        for k in q.sources
                    ]),
                )
            )
        )  # fmt: skip
    key, desc = _sort_key(q.sort)
    tie = _bytewise(conn, ses.c.id)
    if q.cursor is not None:
        position = tuple_(key, tie)
        bound = tuple_(literal(q.cursor.sort_value), literal(q.cursor.session_id))
        where.append(position < bound if desc else position > bound)
    order = [key.desc(), tie.desc()] if desc else [key.asc(), tie.asc()]
    rows = conn.execute(
        select(p.c.id.label("project_id"), ses, key.label("sort_value"))
        .select_from(ses.join(p, p.c.pk == ses.c.project_pk))
        .where(*where)
        .order_by(*order)
        .limit(q.limit + 1)
    ).all()
    items = [_summary_row(row._mapping) for row in rows[: q.limit]]
    cursor = None
    if len(rows) > q.limit:
        last = rows[q.limit - 1]._mapping
        cursor = SessionCursor(int(last["sort_value"]), last["id"])
    return Page(items, cursor)


def _running(agg: Any) -> Any:
    """Window over the Session's Runs in start order, up to and including the current row."""
    return agg.over(order_by=(r.c.start_ns, r.c.trace_id), rows=(None, 0))


def get_session(
    conn: Connection, project_ids: ProjectScope, session_id: str
) -> SessionDetail | None:
    """Q9b: the Session and its Runs in start order with running totals in SQL (REQ-105.b)."""
    if not project_ids:
        return None
    head = conn.execute(
        select(p.c.id.label("project_id"), ses)
        .select_from(ses.join(p, p.c.pk == ses.c.project_pk))
        .where(ses.c.id == session_id, p.c.id.in_(sorted(project_ids)), ses.c.run_count > 0)
    ).first()
    if head is None:
        return None
    stmt = (
        _summary_select()
        .add_columns(
            _running(func.sum(r.c.input_tokens + r.c.output_tokens)).label("cum_tokens"),
            _running(func.sum(func.coalesce(r.c.cost_nusd, 0))).label("cum_cost"),
            _running(func.max(r.c.unpriced_calls)).label("cum_unpriced"),
        )
        .where(r.c.session_pk == head._mapping["pk"])
        .order_by(r.c.start_ns, r.c.trace_id)
    )
    runs = [
        SessionRun(
            run=_summary(row),
            cumulative_tokens=int(row._mapping["cum_tokens"]),
            cumulative_cost_nusd=int(row._mapping["cum_cost"]),
            cumulative_includes_unpriced=int(row._mapping["cum_unpriced"]) > 0,
        )
        for row in conn.execute(stmt).all()
    ]
    return SessionDetail(_summary_row(head._mapping), runs)
