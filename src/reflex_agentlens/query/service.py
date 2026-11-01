"""``QueryService``: the read API used by pages and host apps (plan tasks P4-02.x; API §6.1).

Every method takes a :class:`~reflex_agentlens.query.dto.Viewer` and checks Project scope before
touching storage (REQ-191.c). Storage types never leave this module: results are DTOs built in
:mod:`reflex_agentlens.query.convert`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from reflex_agentlens.core.errors import NotFound, ValidationError
from reflex_agentlens.core.ids import span_id_from_hex, trace_id_from_hex
from reflex_agentlens.query import convert, dto
from reflex_agentlens.query.cursor import CursorCodec
from reflex_agentlens.query.filters import RunFilter
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


class QueryService:
    """Reads (API §6.1). ``cursor_key`` signs keyset cursors (the runtime passes the session
    secret); ``clock_ns`` resolves relative times such as ``now-24h``."""

    def __init__(
        self,
        store: StorageBackend,
        cursor_key: bytes,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.store = store
        self.cursors = CursorCodec(cursor_key)
        self.clock_ns = clock_ns

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

    def _graph(self, detail: r.TraceDetail) -> dto.AgentGraph | None:
        """The agent graph (P4-04.1); ``None`` until it is wired."""
        return None

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
