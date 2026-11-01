"""``QueryService``: the read API used by pages and host apps (plan tasks P4-02.x; API §6.1).

Every method takes a :class:`~reflex_agentlens.query.dto.Viewer` and checks Project scope before
touching storage (REQ-191.c). Storage types never leave this module: results are DTOs built in
:mod:`reflex_agentlens.query.convert`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from reflex_agentlens.core.errors import Forbidden, NotFound, StorageUnavailable, ValidationError
from reflex_agentlens.core.ids import span_id_from_hex, trace_id_from_hex
from reflex_agentlens.query import convert, dto
from reflex_agentlens.query.cursor import CursorCodec
from reflex_agentlens.query.filters import RunFilter
from reflex_agentlens.query.graph import build_graph
from reflex_agentlens.query.scope import (
    check_project,
    parse_source_label,
    resolve_projects,
    run_query,
    time_range,
)
from reflex_agentlens.query.tree import build_tree, error_info, llm_messages, page, tool_io, totals
from reflex_agentlens.store import records as r
from reflex_agentlens.store.protocol import StorageBackend

__all__ = ["MAX_TRACE_PAGE", "QueryService"]

MAX_TRACE_PAGE = 2000
MAX_FACETS = 1000
TOP_TOOLS = 10
_BUCKETS = {"auto": None, "minute": r.MINUTE_NS, "hour": r.HOUR_NS, "day": r.DAY_NS}

log = logging.getLogger(__name__)


class QueryService:
    """Reads (API §6.1). ``cursor_key`` signs keyset cursors (the runtime passes the session
    secret); ``clock_ns`` resolves relative times such as ``now-24h``."""

    def __init__(
        self,
        store: StorageBackend,
        cursor_key: bytes,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        size_limit_bytes: int | None = None,
    ) -> None:
        self.store = store
        self.cursors = CursorCodec(cursor_key)
        self.clock_ns = clock_ns
        self.size_limit_bytes = size_limit_bytes  # [retention] storage_size_limit (REQ-173.a)

    # ── helpers ──────────────────────────────────────────────────────────────────────────────

    async def _sources(
        self, scope: frozenset[str], labels: Sequence[str]
    ) -> tuple[r.SourceKey, ...]:
        if not labels:
            return ()
        if not scope:
            return tuple(parse_source_label(label) for label in labels)
        known = [v.source for v in await self.store.list_sources(scope)]
        return tuple(parse_source_label(label, known) for label in labels)

    def _key(self, viewer: dto.Viewer, run: dto.RunRef) -> r.RunKey:
        check_project(viewer, run.project_id)
        return r.RunKey(run.project_id, trace_id_from_hex(run.trace_id))

    # ── runs ─────────────────────────────────────────────────────────────────────────────────

    async def list_runs(
        self,
        viewer: dto.Viewer,
        filter: RunFilter,
        sort: dto.RunSort = "start_desc",
        cursor: str | None = None,
        limit: int = r.DEFAULT_PAGE_SIZE,
    ) -> dto.Page[dto.RunSummary]:
        """REQ-100, REQ-101: filtered, sorted, keyset-paginated Runs."""
        scope = await resolve_projects(self.store, viewer, filter.project_ids)
        position = None if cursor is None else self.cursors.decode_run(sort, cursor)
        sources = await self._sources(scope, filter.sources)
        query = run_query(
            filter, scope, self.clock_ns(), sources=sources, sort=sort, cursor=position, limit=limit
        )
        result = await self.store.list_runs(query)
        nxt = result.next_cursor
        return dto.Page[dto.RunSummary](
            items=[convert.run_summary(x) for x in result.items],
            next_cursor=None if nxt is None else self.cursors.encode_run(sort, nxt),
        )

    async def resolve_trace(
        self, viewer: dto.Viewer, trace_id: str, project_slug: str | None = None
    ) -> list[dto.RunRef]:
        """REQ-103: Runs with this trace ID among the allowed Projects (>1 = disambiguation)."""
        raw = trace_id_from_hex(trace_id.strip().lower())
        scope = await resolve_projects(self.store, viewer, [project_slug] if project_slug else [])
        keys = await self.store.find_runs_by_trace_id(scope, raw)
        return [dto.RunRef(project_id=k.project_id, trace_id=raw.hex()) for k in keys]

    async def get_trace(
        self, viewer: dto.Viewer, run: dto.RunRef, offset: int = 0, limit: int = MAX_TRACE_PAGE
    ) -> dto.TraceDetail:
        """REQ-103.a: the Run, one page of its span tree, totals and every LLM call."""
        if offset < 0:
            raise ValidationError("offset", "negative")
        if not 1 <= limit <= MAX_TRACE_PAGE:
            raise ValidationError("limit", f"must be between 1 and {MAX_TRACE_PAGE}")
        detail = await self.store.get_trace(self._key(viewer, run))
        if detail is None:
            raise NotFound("trace")
        tree = build_tree(detail.spans)
        nodes, next_offset = page(tree.nodes, offset, limit)
        return dto.TraceDetail(
            run=convert.run_summary(detail.run),
            spans=nodes,
            span_offset=offset,
            next_offset=next_offset,
            graph=self._graph(detail),
            totals=totals(detail.run),
            llm_calls=tree.llm_calls,
        )

    def _graph(self, detail: r.TraceDetail, *, full: bool = False) -> dto.AgentGraph | None:
        """REQ-153: the executed agent graph of a GenAI Run (``None`` otherwise)."""
        return build_graph(detail.spans, full=full) if detail.run.is_genai else None

    async def get_graph(
        self, viewer: dto.Viewer, run: dto.RunRef, *, full: bool = False
    ) -> dto.AgentGraph | None:
        """REQ-153.d: the graph again, uncollapsed when ``full`` (the "full view" action)."""
        detail = await self.store.get_trace(self._key(viewer, run))
        if detail is None:
            raise NotFound("trace")
        return self._graph(detail, full=full)

    async def list_llm_calls(self, viewer: dto.Viewer, run: dto.RunRef) -> list[dto.LLMCallView]:
        """REQ-104: every LLM call with normalized messages, in start order."""
        calls = await self.store.list_llm_calls(self._key(viewer, run))
        return [
            convert.llm_call_view(c.span_id, c.call, llm_messages(c.attributes, c.events))
            for c in calls
        ]

    async def get_span_raw(self, viewer: dto.Viewer, run: dto.RunRef, span_id: str) -> dto.SpanRaw:
        """REQ-151.e: the stored attributes, resource, events and links of one span."""
        raw = await self.store.get_span_raw(self._key(viewer, run), span_id_from_hex(span_id))
        if raw is None:
            raise NotFound("span")
        return convert.span_raw(raw)

    async def get_span_detail(
        self, viewer: dto.Viewer, run: dto.RunRef, span_id: str
    ) -> dto.SpanDetail:
        """REQ-151.a-e: the span drawer in one call (messages, tool I/O, error, raw)."""
        key = self._key(viewer, run)
        sid = span_id_from_hex(span_id)
        detail = await self.store.get_trace(key)
        raw = await self.store.get_span_raw(key, sid)
        if detail is None or raw is None:
            raise NotFound("span")
        span = next((sp for sp in detail.spans if sp.span_id == sid), None)
        node = next((n for n in build_tree(detail.spans).nodes if n.span_id == sid.hex()), None)
        if span is None or node is None:
            raise NotFound("span")
        llm = None
        if span.llm_call is not None:
            messages = llm_messages(raw.attributes, raw.events)
            llm = convert.llm_call_view(sid, span.llm_call, messages)
        tool = None
        if span.tool_call is not None:
            args, result = tool_io(raw)
            tool = convert.tool_call_view(sid, span.tool_call, args, result)
        return dto.SpanDetail(
            span=node,
            llm=llm,
            tool=tool,
            error=error_info(span.status_code, span.status_message, raw),
            raw=convert.span_raw(raw),
        )

    # ── sessions ─────────────────────────────────────────────────────────────────────────────

    async def list_sessions(
        self,
        viewer: dto.Viewer,
        filter: dto.SessionFilter,
        sort: dto.SessionSort = "last_desc",
        cursor: str | None = None,
        limit: int = r.DEFAULT_PAGE_SIZE,
    ) -> dto.Page[dto.SessionSummary]:
        """REQ-105.a: Sessions by Project, Source and time range, keyset-paginated."""
        if not 1 <= limit <= r.MAX_PAGE_SIZE:
            raise ValidationError("limit", f"must be between 1 and {r.MAX_PAGE_SIZE}")
        scope = await resolve_projects(self.store, viewer, filter.project_ids)
        position = None if cursor is None else self.cursors.decode_session(sort, cursor)
        query = r.SessionQuery(
            project_ids=scope,
            sources=await self._sources(scope, filter.sources),
            time_range=time_range(filter.time_from, filter.time_to, self.clock_ns()),
            sort=sort,
            cursor=position,
            limit=limit,
        )
        result = await self.store.list_sessions(query)
        nxt = result.next_cursor
        return dto.Page[dto.SessionSummary](
            items=[convert.session_summary(x) for x in result.items],
            next_cursor=None if nxt is None else self.cursors.encode_session(sort, nxt),
        )

    async def get_session(self, viewer: dto.Viewer, session_id: str) -> dto.SessionDetail:
        """REQ-105.b: Runs in chronological order with cumulative tokens and cost."""
        scope = await resolve_projects(self.store, viewer)
        detail = await self.store.get_session(scope, session_id)
        if detail is None:
            raise NotFound("session")
        return convert.session_detail(detail)

    # ── selectors ────────────────────────────────────────────────────────────────────────────

    async def facets(
        self,
        viewer: dto.Viewer,
        field: dto.FacetField,
        scope: dto.AggScope,
        limit: int = 100,
    ) -> list[str]:
        """REQ-145.b: distinct models, tools, statuses, sources or agents for filter pickers."""
        if not 1 <= limit <= MAX_FACETS:
            raise ValidationError("limit", f"must be between 1 and {MAX_FACETS}")
        projects = await resolve_projects(self.store, viewer, scope.project_ids)
        if not projects:
            return []
        tr = time_range(scope.time_from, scope.time_to, self.clock_ns(), required=True)
        assert tr is not None  # noqa: S101 - required=True always returns a range
        return await self.store.distinct_values(projects, field, tr, limit)

    async def list_projects(self, viewer: dto.Viewer) -> list[dto.ProjectRef]:
        """REQ-130.a: the Project selector, limited to the viewer's Projects."""
        allowed = viewer.allowed_project_ids
        return [
            convert.project_ref(p)
            for p in await self.store.list_projects()
            if allowed is None or p.id in allowed
        ]

    async def list_sources(
        self, viewer: dto.Viewer, project_ids: Sequence[str] = ()
    ) -> list[dto.SourceView]:
        """REQ-130.a: the Source selector."""
        scope = await resolve_projects(self.store, viewer, project_ids)
        if not scope:
            return []
        return [convert.source_view(v) for v in await self.store.list_sources(scope)]

    async def get_project(self, viewer: dto.Viewer, project_id: str) -> dto.Project:
        check_project(viewer, project_id)
        for p in await self.store.list_projects():
            if p.id == project_id:
                return convert.project(p)
        raise NotFound("project")

    # ── dashboard and storage ────────────────────────────────────────────────────────────────

    async def dashboard(self, viewer: dto.Viewer, scope: dto.AggScope) -> dto.DashboardData:
        """REQ-106, REQ-107: every aggregate, fetched concurrently. A failing aggregate is
        listed in ``failed`` and the others are still returned (REQ-130.d)."""
        projects = await resolve_projects(self.store, viewer, scope.project_ids)
        tr = time_range(scope.time_from, scope.time_to, self.clock_ns(), required=True)
        assert tr is not None  # noqa: S101 - required=True always returns a range
        q = r.AggQuery(
            project_ids=projects,
            time_range=tr,
            sources=await self._sources(projects, scope.sources),
            bucket_ns=_BUCKETS[scope.bucket],
        )
        base: dict[str, Any] = {
            "project_ids": sorted(projects),
            "time_from_ns": tr.start_ns,
            "time_to_ns": tr.end_ns,
            "bucket_ns": q.bucket,
        }
        if not projects:
            return dto.DashboardData(**base)
        s = self.store
        panels: dict[str, Awaitable[Any]] = {
            "kpis": _then(s.agg_kpis(q), convert.kpis),
            "runs_over_time": _then(s.agg_runs_over_time(q), convert.time_series),
            "model_usage": _then(s.agg_tokens_cost_by_model(q), convert.model_usage),
            "latency_by_operation": _then(
                s.agg_latency_percentiles(q, "operation"), convert.latency_rows
            ),
            "latency_by_node": _then(s.agg_latency_percentiles(q, "node"), convert.latency_rows),
            "tools": _then(s.agg_tool_stats(q, TOP_TOOLS), convert.tool_stats),
            "by_source": _then(s.agg_by_source(q), convert.source_usage),
            "approvals": _then(s.agg_approvals(q), convert.approval_stats),
            "metric_tokens": _then(s.agg_metric_token_usage(q), convert.time_series),
        }
        results = await asyncio.gather(*panels.values(), return_exceptions=True)
        failed: list[str] = []
        for name, value in zip(panels, results, strict=True):
            if isinstance(value, BaseException):
                if not isinstance(value, Exception):
                    raise value
                log.warning("dashboard aggregate %s failed: %s", name, type(value).__name__)
                failed.append(name)
            else:
                base[name] = value
        if len(failed) == len(panels):
            raise StorageUnavailable("dashboard aggregates failed")
        return dto.DashboardData(**base, failed=failed)

    async def storage_status(self, viewer: dto.Viewer) -> dto.StorageStatus:
        """REQ-173: instance-wide usage, size limit and last purge. Instance-wide numbers are
        shown only to viewers who see every Project."""
        if viewer.allowed_project_ids is not None:
            raise Forbidden("instance-wide storage status")
        usage = await self.store.storage_usage()
        return dto.StorageStatus(
            estimated_bytes=usage.estimated_bytes,
            physical_bytes=usage.physical_bytes,
            size_limit_bytes=self.size_limit_bytes,
            runs=usage.runs,
            spans=usage.spans,
            last_purge=convert.purge_view(await self.store.last_purge()),
        )


async def _then(result: Awaitable[Any], fn: Callable[[Any], Any]) -> Any:
    return fn(await result)
