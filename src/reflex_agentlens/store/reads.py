"""Run and trace reads shared by the SQL backends (plan task P2-07.1; Data Model Q1-Q5b).

Every read is restricted to a :data:`~reflex_agentlens.store.records.ProjectScope`; an empty scope
returns nothing, never everything (API §5). User-supplied values are always bound parameters,
attribute keys are validated against the TD §6.1 pattern, and full-text input goes through the
DD-006 builder (REQ-325.b).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import (
    ColumnElement,
    Connection,
    Select,
    and_,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.dialect import SqlDialectOps, ops_for
from reflex_agentlens.store.records import (
    MAX_PAGE_SIZE,
    ApprovalRow,
    FacetField,
    GraphNodeRow,
    LLMCallRow,
    LLMCallView,
    Page,
    ProjectScope,
    RunCursor,
    RunKey,
    RunQuery,
    RunSummary,
    SourceKey,
    SourceView,
    SpanRaw,
    TimeRange,
    ToolCallRow,
    TraceDetail,
    TraceSpan,
)

__all__ = [
    "ATTRIBUTE_KEY",
    "ReadMethods",
    "distinct_values",
    "find_runs_by_trace_id",
    "get_span_raw",
    "get_trace",
    "list_llm_calls",
    "list_runs",
    "list_sources",
]

ATTRIBUTE_KEY = re.compile(r"^[A-Za-z0-9_.\-:]{1,256}$")  # Tech Design §6.1
MAX_SORT = 2**62  # sentinel for NULL sort keys in ascending order (NULLs always last)

r, p, src, ses, sp = s.runs, s.projects, s.sources, s.sessions, s.spans


def _bytewise(conn: Connection, column: Any) -> Any:
    """Order text by code point on both backends (PostgreSQL defaults to the locale)."""
    return column.collate("C") if conn.dialect.name == "postgresql" else column


def _scope_pks(project_ids: ProjectScope) -> Any:
    return select(p.c.pk).where(p.c.id.in_(sorted(project_ids))).scalar_subquery()


def _summary_select() -> Select[Any]:
    return select(
        p.c.id.label("project_id"),
        r,
        src.c.service_name,
        src.c.service_namespace,
        ses.c.id.label("session_id"),
    ).select_from(
        r.join(p, p.c.pk == r.c.project_pk)
        .outerjoin(src, src.c.pk == r.c.source_pk)
        .outerjoin(ses, ses.c.pk == r.c.session_pk)
    )


def _summary(row: Any) -> RunSummary:
    m = row._mapping
    source = (
        SourceKey(m["service_name"], m["service_namespace"])
        if m["service_name"] is not None
        else None
    )
    return RunSummary(
        project_id=m["project_id"],
        trace_id=bytes(m["trace_id"]),
        start_ns=m["start_ns"],
        end_ns=m["end_ns"],
        duration_ns=m["duration_ns"],
        last_span_ns=m["last_span_ns"],
        status=m["status"],
        root_name=m["root_name"],
        agent_name=m["agent_name"],
        source=source,
        source_count=m["source_count"],
        models=list(m["models"] or []),
        input_tokens=m["input_tokens"],
        output_tokens=m["output_tokens"],
        cached_input_tokens=m["cached_input_tokens"],
        usage_source=m["usage_source"],
        cost_nusd=m["cost_nusd"],
        unpriced_calls=m["unpriced_calls"],
        llm_calls=m["llm_calls"],
        tool_calls=m["tool_calls"],
        error_spans=m["error_spans"],
        span_count=m["span_count"],
        missing_parents=m["missing_parents"],
        session_id=m["session_id"],
        is_genai=bool(m["is_genai"]),
    )


# ── Q1-Q3: run list ──────────────────────────────────────────────────────────────────────────


def _sort_key(sort: str) -> tuple[ColumnElement[Any], bool]:
    field, direction = sort.rsplit("_", 1)
    desc = direction == "desc"
    column: ColumnElement[Any] = {
        "start": r.c.start_ns,
        "duration": r.c.duration_ns,
        "tokens": r.c.input_tokens + r.c.output_tokens,
        "cost": r.c.cost_nusd,
    }[field]
    if field != "start":  # NULLs last in both directions
        column = func.coalesce(column, -1 if desc else MAX_SORT)
    return column, desc


def _range(column: ColumnElement[Any], rng: Any, field: str) -> list[ColumnElement[bool]]:
    if rng is None:
        return []
    if rng.min is not None and rng.max is not None and rng.min > rng.max:
        raise ValidationError(field, "range minimum exceeds maximum")
    out = []
    if rng.min is not None:
        out.append(column >= rng.min)
    if rng.max is not None:
        out.append(column <= rng.max)
    return out


def _attr_clause(ops: SqlDialectOps, pred: Any) -> ColumnElement[bool]:
    if not ATTRIBUTE_KEY.match(pred.key):
        raise ValidationError("attributes", "invalid attribute key")
    if pred.op == "eq" and (pred.value is None or isinstance(pred.value, list | dict)):
        raise ValidationError("attributes", "equality needs a scalar value")
    root = sp.alias("root_attr")
    if pred.scope == "resource":
        res = s.resources.alias("res_attr")
        column: ColumnElement[Any] = res.c.attributes
        base = select(literal(1)).select_from(root.join(res, res.c.pk == root.c.resource_pk))
    else:
        column = root.c.attributes
        base = select(literal(1)).select_from(root)
    cond = (
        ops.json_has(column, pred.key)
        if pred.op == "exists"
        else ops.json_equals(column, pred.key, pred.value)
    )
    return exists(base.where(root.c.pk == r.c.root_span_pk, cond))


def _text_clause(ops: SqlDialectOps, query: str) -> ColumnElement[bool] | None:
    fts = ops.fts_query(query)
    if fts is None:
        return None
    if ops.name == "sqlite":
        sub = text(
            f"SELECT s.run_pk FROM spans s JOIN span_search ON span_search.rowid = s.pk "  # noqa: S608
            f"WHERE {fts.sql}"
        ).bindparams(**fts.params)
        return r.c.pk.in_(sub.columns(s.spans.c.run_pk))
    sub = text(f"SELECT span_search.run_pk FROM span_search WHERE {fts.sql}").bindparams(  # noqa: S608
        **fts.params
    )
    return r.c.pk.in_(sub.columns(s.spans.c.run_pk))


def list_runs(conn: Connection, q: RunQuery) -> Page[RunSummary]:
    """Q1-Q3: filtered, sorted, keyset-paginated Runs (REQ-100, REQ-101)."""
    if not 1 <= q.limit <= MAX_PAGE_SIZE:
        raise ValidationError("limit", f"must be between 1 and {MAX_PAGE_SIZE}")
    if not q.project_ids:
        return Page([], None)
    ops = ops_for(conn.dialect.name)
    where: list[ColumnElement[bool]] = [r.c.project_pk.in_(_scope_pks(q.project_ids))]
    if q.sources:
        where.append(
            or_(*[
                and_(src.c.service_name == k.service_name,
                     src.c.service_namespace == k.service_namespace)
                for k in q.sources
            ])
        )  # fmt: skip
    if q.time_range is not None:
        where += [r.c.start_ns >= q.time_range.start_ns, r.c.start_ns < q.time_range.end_ns]
    if q.statuses:
        where.append(r.c.status.in_(q.statuses))
    if q.is_genai is not None:
        where.append(r.c.is_genai.is_(q.is_genai))
    if q.models:
        lc = s.llm_calls
        where.append(exists().where(lc.c.run_pk == r.c.pk, lc.c.model.in_(q.models)))
    if q.tools:
        tc = s.tool_calls
        where.append(exists().where(tc.c.run_pk == r.c.pk, tc.c.tool_name.in_(q.tools)))
    where += _range(r.c.duration_ns, q.duration_ns, "duration")
    where += _range(r.c.input_tokens + r.c.output_tokens, q.tokens, "tokens")
    cost = _range(r.c.cost_nusd, q.cost_nusd, "cost")
    if cost:  # REQ-101.e: unpriced Runs are outside cost ranges unless asked for
        priced = and_(r.c.cost_nusd.is_not(None), r.c.unpriced_calls == 0, *cost)
        where.append(or_(priced, r.c.unpriced_calls > 0) if q.include_unpriced_in_cost_range
                     else priced)  # fmt: skip
    where += [_attr_clause(ops, pred) for pred in q.attributes]
    if q.text:
        clause = _text_clause(ops, q.text)
        if clause is not None:
            where.append(clause)
    if q.session_id is not None:
        where.append(r.c.session_pk == select(ses.c.pk).where(ses.c.id == q.session_id)
                     .scalar_subquery())  # fmt: skip

    key, desc = _sort_key(q.sort)
    order_key = (key, r.c.trace_id, p.c.id)
    if q.cursor is not None:
        c = q.cursor
        position = tuple_(*order_key)
        bound = tuple_(literal(c.sort_value), literal(c.trace_id), literal(c.project_id))
        where.append(position < bound if desc else position > bound)
    order = [col.desc() if desc else col.asc() for col in order_key]
    stmt = (
        _summary_select()
        .add_columns(key.label("sort_value"))
        .where(*where)
        .order_by(*order)
        .limit(q.limit + 1)
    )
    rows = conn.execute(stmt).all()
    items = [_summary(row) for row in rows[: q.limit]]
    next_cursor = None
    if len(rows) > q.limit:
        last = rows[q.limit - 1]._mapping
        next_cursor = RunCursor(
            int(last["sort_value"]), bytes(last["trace_id"]), last["project_id"]
        )
    return Page(items, next_cursor)


# ── Q4: resolve a trace ID ───────────────────────────────────────────────────────────────────


def find_runs_by_trace_id(
    conn: Connection, project_ids: ProjectScope, trace_id: bytes
) -> list[RunKey]:
    if not project_ids:
        return []
    rows = conn.execute(
        select(p.c.id)
        .select_from(r.join(p, p.c.pk == r.c.project_pk))
        .where(r.c.trace_id == trace_id, p.c.id.in_(sorted(project_ids)))
        .order_by(p.c.id)
    ).scalars()
    return [RunKey(pid, trace_id) for pid in rows]


def _run_pk(conn: Connection, key: RunKey) -> int | None:
    return conn.execute(
        select(r.c.pk)
        .select_from(r.join(p, p.c.pk == r.c.project_pk))
        .where(p.c.id == key.project_id, r.c.trace_id == key.trace_id)
    ).scalar()


# ── Q5 / Q5b: trace detail and raw span ──────────────────────────────────────────────────────


def _derived(row: Any, table: Any, cls: type, prefix: str) -> Any:
    m = row._mapping
    if m[f"{prefix}_present"] is None:
        return None
    names = [c.name for c in table.columns if c.name not in {"span_pk", "run_pk", "project_pk"}]
    values = {n: m[f"{prefix}_{n}"] for n in names}
    for n, v in values.items():
        if isinstance(v, int) and n in {"unpriced", "content_dropped"}:
            values[n] = bool(v)
    return cls(**values)


def get_trace(conn: Connection, key: RunKey) -> TraceDetail | None:
    """Q5: the Run and every span with its derived rows, ordered by start time (REQ-103.a)."""
    page = conn.execute(
        _summary_select().where(p.c.id == key.project_id, r.c.trace_id == key.trace_id)
    ).first()
    if page is None:
        return None
    run = _summary(page)
    run_pk = page._mapping["pk"]
    joins = sp.join(src, src.c.pk == sp.c.source_pk)
    cols: list[Any] = [sp, src.c.service_name, src.c.service_namespace]
    derived = (
        (s.llm_calls, LLMCallRow, "llm"),
        (s.tool_calls, ToolCallRow, "tool"),
        (s.approvals, ApprovalRow, "appr"),
        (s.graph_nodes, GraphNodeRow, "node"),
    )
    for table, _, prefix in derived:
        joins = joins.outerjoin(table, table.c.span_pk == sp.c.pk)
        cols.append(table.c.span_pk.label(f"{prefix}_present"))
        cols += [
            c.label(f"{prefix}_{c.name}")
            for c in table.columns
            if c.name not in {"span_pk", "run_pk", "project_pk"}
        ]
    rows = conn.execute(
        select(*cols).select_from(joins).where(sp.c.run_pk == run_pk)
        .order_by(sp.c.start_ns, sp.c.span_id)
    ).all()  # fmt: skip
    spans = []
    for row in rows:
        m = row._mapping
        spans.append(
            TraceSpan(
                span_id=bytes(m["span_id"]),
                parent_span_id=None if m["parent_span_id"] is None else bytes(m["parent_span_id"]),
                name=m["name"],
                category=m["category"],
                otel_kind=m["otel_kind"],
                start_ns=m["start_ns"],
                end_ns=m["end_ns"],
                status_code=m["status_code"],
                status_message=m["status_message"],
                flags=m["flags"],
                source=SourceKey(m["service_name"], m["service_namespace"]),
                agent_name=m["agent_name"],
                llm_call=_derived(row, s.llm_calls, LLMCallRow, "llm"),
                tool_call=_derived(row, s.tool_calls, ToolCallRow, "tool"),
                approval=_derived(row, s.approvals, ApprovalRow, "appr"),
                graph_node=_derived(row, s.graph_nodes, GraphNodeRow, "node"),
            )
        )
    return TraceDetail(run, spans)


def get_span_raw(conn: Connection, key: RunKey, span_id: bytes) -> SpanRaw | None:
    """Q5b: raw attributes, resource, events and links of one span (REQ-151.e, REQ-030.b)."""
    run_pk = _run_pk(conn, key)
    if run_pk is None:
        return None
    res = s.resources
    row = conn.execute(
        select(sp, res.c.attributes.label("resource_attributes"))
        .select_from(sp.join(res, res.c.pk == sp.c.resource_pk))
        .where(sp.c.run_pk == run_pk, sp.c.span_id == span_id)
    ).first()
    if row is None:
        return None
    m = row._mapping
    return SpanRaw(
        span_id=bytes(m["span_id"]),
        attributes=m["attributes"],
        resource=m["resource_attributes"],
        scope_name=m["scope_name"],
        scope_version=m["scope_version"],
        events=m["events"],
        links=m["links"],
        dropped_keys=m["dropped_keys"],
    )


def list_llm_calls(conn: Connection, key: RunKey) -> list[LLMCallView]:
    """LLM calls of one Run with their spans' attributes and events (REQ-104)."""
    run_pk = _run_pk(conn, key)
    if run_pk is None:
        return []
    lc = s.llm_calls
    names = [c.name for c in lc.columns if c.name not in {"span_pk", "run_pk", "project_pk"}]
    rows = conn.execute(
        select(sp.c.span_id, sp.c.attributes, sp.c.events, *[lc.c[n] for n in names])
        .select_from(lc.join(sp, sp.c.pk == lc.c.span_pk))
        .where(lc.c.run_pk == run_pk)
        .order_by(lc.c.start_ns, sp.c.span_id)
    ).all()
    out = []
    for row in rows:
        m = row._mapping
        values = {n: m[n] for n in names}
        values["unpriced"] = bool(values["unpriced"])
        values["content_dropped"] = bool(values["content_dropped"])
        out.append(
            LLMCallView(bytes(m["span_id"]), LLMCallRow(**values), m["attributes"], m["events"])
        )
    return out


# ── sources and facets ───────────────────────────────────────────────────────────────────────


def list_sources(conn: Connection, project_ids: ProjectScope) -> list[SourceView]:
    if not project_ids:
        return []
    rows = conn.execute(
        select(p.c.id, src.c.service_namespace, src.c.service_name, src.c.first_seen_ns,
               src.c.last_seen_ns)
        .select_from(src.join(p, p.c.pk == src.c.project_pk))
        .where(p.c.id.in_(sorted(project_ids)))
        .order_by(
            p.c.id,
            _bytewise(conn, src.c.service_namespace),
            _bytewise(conn, src.c.service_name),
        )
    ).all()  # fmt: skip
    return [
        SourceView(pid, SourceKey(name, ns), first, last) for pid, ns, name, first, last in rows
    ]


def distinct_values(
    conn: Connection,
    project_ids: ProjectScope,
    field: FacetField,
    time_range: TimeRange,
    limit: int,
) -> list[str]:
    """Distinct facet values for filter UIs (models, tools, statuses, sources, agents)."""
    if not project_ids or limit < 1:
        return []
    scope = _scope_pks(project_ids)
    lc, tc = s.llm_calls, s.tool_calls
    stmt: Select[Any]
    if field == "model":
        stmt = select(lc.c.model.label("v")).where(
            lc.c.project_pk.in_(scope), lc.c.start_ns >= time_range.start_ns,
            lc.c.start_ns < time_range.end_ns,
        )  # fmt: skip
    elif field == "tool":
        stmt = select(tc.c.tool_name.label("v")).where(
            tc.c.project_pk.in_(scope), tc.c.start_ns >= time_range.start_ns,
            tc.c.start_ns < time_range.end_ns,
        )  # fmt: skip
    elif field in ("status", "agent"):
        column = r.c.status if field == "status" else r.c.agent_name
        stmt = select(column.label("v")).where(
            r.c.project_pk.in_(scope), r.c.start_ns >= time_range.start_ns,
            r.c.start_ns < time_range.end_ns, column.is_not(None),
        )  # fmt: skip
    elif field == "source":
        label = func.coalesce(
            func.nullif(src.c.service_namespace, "") + "/" + src.c.service_name,
            src.c.service_name,
        )
        stmt = select(label.label("v")).where(
            src.c.project_pk.in_(scope), src.c.last_seen_ns >= time_range.start_ns,
            src.c.first_seen_ns < time_range.end_ns,
        )  # fmt: skip
    else:
        raise ValidationError("field", f"unknown facet {field!r}")
    sub = stmt.distinct().subquery()
    ordered = select(sub.c.v).order_by(_bytewise(conn, sub.c.v)).limit(limit)
    return list(conn.execute(ordered).scalars())


# ── async surface shared by both backends ────────────────────────────────────────────────────

_R = TypeVar("_R")


class ReadMethods:
    """Mixin: the P2-07.1 protocol methods, run on the backend's read pool."""

    @property
    def read_engine(self) -> AsyncEngine:  # pragma: no cover - overridden by every backend
        raise NotImplementedError

    async def _on_read_pool(self, fn: Callable[..., _R], *args: Any) -> _R:
        async with self.read_engine.connect() as conn:
            return await conn.run_sync(fn, *args)

    async def list_runs(self, query: RunQuery) -> Page[RunSummary]:
        return await self._on_read_pool(list_runs, query)

    async def find_runs_by_trace_id(self, scope: ProjectScope, trace_id: bytes) -> list[RunKey]:
        return await self._on_read_pool(find_runs_by_trace_id, scope, trace_id)

    async def get_trace(self, key: RunKey) -> TraceDetail | None:
        return await self._on_read_pool(get_trace, key)

    async def get_span_raw(self, key: RunKey, span_id: bytes) -> SpanRaw | None:
        return await self._on_read_pool(get_span_raw, key, span_id)

    async def list_llm_calls(self, key: RunKey) -> list[LLMCallView]:
        return await self._on_read_pool(list_llm_calls, key)

    async def list_sources(self, scope: ProjectScope) -> list[SourceView]:
        return await self._on_read_pool(list_sources, scope)

    async def distinct_values(
        self, scope: ProjectScope, field: FacetField, time_range: TimeRange, limit: int = 100
    ) -> list[str]:
        return await self._on_read_pool(distinct_values, scope, field, time_range, limit)
