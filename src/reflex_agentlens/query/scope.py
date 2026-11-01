"""Viewer scope and filter → storage query translation (plan task P4-01.2; API §6, REQ-191.c).

Every service method resolves its Projects here **first**: a requested Project (slug or ID)
outside ``viewer.allowed_project_ids`` raises ``Forbidden``; an unknown one raises
``ValidationError("project")``; an empty request means every allowed Project.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Protocol

from reflex_agentlens.core.errors import Forbidden, ValidationError
from reflex_agentlens.core.money import usd_to_nusd
from reflex_agentlens.core.timeutil import NS_PER_MS
from reflex_agentlens.query.dto import Range, RunSort, Viewer
from reflex_agentlens.query.filters import RunFilter
from reflex_agentlens.query.timeexpr import resolve_time
from reflex_agentlens.store import records as r

__all__ = [
    "MAX_NS",
    "ProjectLister",
    "check_project",
    "parse_source_label",
    "resolve_projects",
    "run_query",
    "source_label",
    "time_range",
]

MAX_NS = 2**63 - 1


class ProjectLister(Protocol):
    async def list_projects(self) -> list[r.Project]: ...


def check_project(viewer: Viewer, project_id: str) -> None:
    """``Forbidden`` unless the viewer may see ``project_id``."""
    allowed = viewer.allowed_project_ids
    if allowed is not None and project_id not in allowed:
        raise Forbidden("project not allowed")


async def resolve_projects(
    store: ProjectLister, viewer: Viewer, requested: Sequence[str] = ()
) -> frozenset[str]:
    """Project IDs the call may read: the requested ones (slugs or IDs) or every allowed one."""
    projects = await store.list_projects()
    allowed = viewer.allowed_project_ids
    if not requested:
        ids = {p.id for p in projects}
        return frozenset(ids if allowed is None else ids & allowed)
    by_key = {p.id: p.id for p in projects} | {p.slug: p.id for p in projects}
    out: set[str] = set()
    for key in requested:
        project_id = by_key.get(key)
        if project_id is None:
            # an allowed-list viewer must not learn which Projects exist: same error either way
            if allowed is not None:
                raise Forbidden("project not allowed")
            raise ValidationError("project", "unknown project")
        check_project(viewer, project_id)
        out.add(project_id)
    return frozenset(out)


def source_label(source: r.SourceKey) -> str:
    if source.service_namespace:
        return f"{source.service_namespace}/{source.service_name}"
    return source.service_name


def parse_source_label(label: str, known: Iterable[r.SourceKey] = ()) -> r.SourceKey:
    """A label → its ``SourceKey``; known Sources win, else the first ``/`` splits ``ns/name``."""
    for source in known:
        if source_label(source) == label:
            return source
    ns, sep, name = label.partition("/")
    return r.SourceKey(name, ns) if sep and ns and name else r.SourceKey(label)


def time_range(
    time_from: str | None, time_to: str | None, now_ns: int, *, required: bool = False
) -> r.TimeRange | None:
    """``[from, to)`` in ns; open ends become 0 / ``MAX_NS``. ``from > to`` is a field error."""
    if time_from is None and time_to is None and not required:
        return None
    start = 0 if time_from is None else resolve_time(time_from, now_ns, "from")
    end = MAX_NS if time_to is None else resolve_time(time_to, now_ns, "to")
    if start > end:
        raise ValidationError("from", "after to")
    return r.TimeRange(start, end)


def _range(rng: Range[int] | None, scale: int = 1) -> r.Range | None:
    if rng is None:
        return None
    return r.Range(
        None if rng.min is None else rng.min * scale,
        None if rng.max is None else rng.max * scale,
    )


def _cost(rng: Range[Decimal] | None) -> r.Range | None:
    if rng is None:
        return None
    return r.Range(
        None if rng.min is None else usd_to_nusd(rng.min),
        None if rng.max is None else usd_to_nusd(rng.max),
    )


def run_query(
    f: RunFilter,
    project_ids: frozenset[str],
    now_ns: int,
    *,
    sources: Iterable[r.SourceKey] = (),
    sort: RunSort = "start_desc",
    cursor: r.RunCursor | None = None,
    limit: int = r.DEFAULT_PAGE_SIZE,
) -> r.RunQuery:
    """``RunFilter`` → ``store.records.RunQuery`` (labels resolved against known Sources)."""
    if not 1 <= limit <= r.MAX_PAGE_SIZE:
        raise ValidationError("limit", f"must be between 1 and {r.MAX_PAGE_SIZE}")
    known = list(sources)
    return r.RunQuery(
        project_ids=project_ids,
        sources=tuple(parse_source_label(label, known) for label in f.sources),
        time_range=time_range(f.time_from, f.time_to, now_ns),
        statuses=tuple(f.statuses),
        is_genai=f.is_genai,
        models=tuple(f.models),
        tools=tuple(f.tools),
        duration_ns=_range(f.duration_ms, NS_PER_MS),
        tokens=_range(f.tokens),
        cost_nusd=_cost(f.cost_usd),
        include_unpriced_in_cost_range=f.include_unpriced_in_cost_range,
        attributes=tuple(
            r.AttrPredicate(key=a.key, scope=a.scope, op=a.op, value=a.value) for a in f.attributes
        ),
        text=f.text,
        session_id=f.session_id,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
