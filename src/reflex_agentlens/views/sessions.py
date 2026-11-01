"""Sessions pages view model (plan task P6-08.1; REQ-105, 160).

- :func:`sessions_query`: the Sessions URL (shared chrome plus ``sort`` and ``cursor``) →
  ``SessionFilter``; never raises on a bad ``sort`` (it falls back to the default).
- :func:`session_rows`, :func:`session_run_rows`: display rows.
- :func:`cumulative_series`: the Session's cumulative tokens and cost, one point per Run, for
  the silverpoint ``area_chart`` (REQ-160.c). Cost is a ``float`` here only because the chart
  draws numbers; every displayed cost is formatted from the ``Decimal``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import get_args
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict

from reflex_agentlens.query.dto import SessionDetail, SessionFilter, SessionSort, SessionSummary
from reflex_agentlens.views.chrome import decode_chrome, encode_chrome
from reflex_agentlens.views.fmt import DASH, fmt_cost, fmt_duration, fmt_int, fmt_time
from reflex_agentlens.views.runs import trace_href

__all__ = [
    "SORTS",
    "SessionRow",
    "SessionRunRow",
    "SessionsQuery",
    "cumulative_series",
    "session_href",
    "session_rows",
    "session_run_rows",
    "sessions_href",
    "sessions_query",
]

SORTS: tuple[str, ...] = get_args(SessionSort)
DEFAULT_SORT = "last_desc"
MAX_CURSOR = 512

Params = Mapping[str, str | Sequence[str]]


@dataclass(frozen=True, slots=True)
class SessionsQuery:
    filter: SessionFilter
    sort: SessionSort = "last_desc"
    cursor: str | None = None
    chrome_pairs: tuple[tuple[str, str], ...] = ()


def _first(params: Params, key: str) -> str | None:
    value = params.get(key)
    if value is None or isinstance(value, str):
        return value
    return value[0] if value else None


def sessions_query(params: Params) -> SessionsQuery:
    chrome = decode_chrome(params)
    sort = _first(params, "sort") or DEFAULT_SORT
    cursor = _first(params, "cursor")
    if cursor is not None and not (0 < len(cursor) <= MAX_CURSOR and cursor.isprintable()):
        cursor = None
    return SessionsQuery(
        filter=SessionFilter(
            project_ids=[chrome.project] if chrome.project else [],
            sources=list(chrome.sources),
            time_from=chrome.from_,
            time_to=chrome.to,
        ),
        sort=sort if sort in SORTS else DEFAULT_SORT,  # type: ignore[arg-type]
        cursor=cursor,
        chrome_pairs=tuple(encode_chrome(chrome)),
    )


def sessions_href(prefix: str, q: SessionsQuery, *, sort: str | None = None,
                  cursor: str | None = None) -> str:  # fmt: skip
    pairs = list(q.chrome_pairs)
    chosen = sort or q.sort
    if chosen != DEFAULT_SORT:
        pairs.append(("sort", chosen))
    if cursor:
        pairs.append(("cursor", cursor))
    base = f"{prefix.rstrip('/')}/sessions"
    return f"{base}?{urlencode(pairs)}" if pairs else base


def session_href(prefix: str, session_id: str) -> str:
    return f"{prefix.rstrip('/')}/sessions/{quote(session_id, safe='')}"


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True)


class SessionRow(_Row):
    session_id: str
    href: str
    external_id: str
    first: str
    last: str
    span: str
    runs: str
    tokens: str
    cost: str


def session_rows(items: Iterable[SessionSummary], prefix: str) -> list[SessionRow]:
    return [
        SessionRow(
            session_id=s.session_id,
            href=session_href(prefix, s.session_id),
            external_id=s.external_id or DASH,
            first=fmt_time(s.first_ns),
            last=fmt_time(s.last_ns),
            span=fmt_duration(max(s.last_ns - s.first_ns, 0)),
            runs=fmt_int(s.run_count),
            tokens=fmt_int(s.total_tokens),
            cost=fmt_cost(s.cost_usd, s.includes_unpriced),
        )
        for s in items
    ]


class SessionRunRow(_Row):
    index: str
    href: str
    start: str
    status: str
    name: str
    duration: str
    tokens: str
    cost: str
    cumulative_tokens: str
    cumulative_cost: str


def session_run_rows(detail: SessionDetail, prefix: str) -> list[SessionRunRow]:
    rows = []
    for i, item in enumerate(detail.runs, start=1):
        run = item.run
        rows.append(
            SessionRunRow(
                index=str(i),
                href=trace_href(prefix, run.project_id, run.trace_id),
                start=fmt_time(run.start_ns),
                status=run.status,
                name=run.agent_name or run.root_name or DASH,
                duration=fmt_duration(run.duration_ns),
                tokens=fmt_int(run.total_tokens),
                cost=fmt_cost(run.cost_usd, run.unpriced),
                cumulative_tokens=fmt_int(item.cumulative_tokens),
                cumulative_cost=fmt_cost(item.cumulative_cost_usd,
                                         item.cumulative_includes_unpriced),
            )
        )  # fmt: skip
    return rows


def cumulative_series(detail: SessionDetail) -> list[dict[str, str | int | float]]:
    """One point per Run in order: ``{x: "#3", tokens, cost}`` (REQ-160.c)."""
    return [
        {
            "x": f"#{i}",
            "tokens": item.cumulative_tokens,
            "cost": float(item.cumulative_cost_usd),
        }
        for i, item in enumerate(detail.runs, start=1)
    ]
