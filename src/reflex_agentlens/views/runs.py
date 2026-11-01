"""Runs page view model (plan tasks P6-04.1, P6-04.2; REQ-101, 102, 131, 140, 145).

- :func:`run_rows`: ``RunSummary`` → the REQ-145.a columns as display text.
- :class:`FilterForm`: the filter bar's fields ↔ the API §6.2 grammar. The URL is the source of
  truth: the form is decoded from it and encoded back through ``query.filters`` (one grammar,
  validated in one place). Project, Sources and time come from the shared chrome.
- :func:`on_notice`: what a live change notice does to the table (REQ-145.c/d).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from reflex_agentlens.query.dto import ChangeNotice, RunRef, RunSummary
from reflex_agentlens.query.filters import (
    RunFilter,
    RunsQuery,
    format_attr,
    parse_runs_query,
    runs_query_string,
)
from reflex_agentlens.views.chrome import Chrome, encode_chrome
from reflex_agentlens.views.fmt import DASH, fmt_cost, fmt_duration, fmt_int, fmt_time

__all__ = [
    "FilterForm",
    "NoticeAction",
    "RunRow",
    "canonical",
    "form_from_query",
    "is_live_first_page",
    "on_notice",
    "query_from_form",
    "ref_key",
    "run_key",
    "run_rows",
    "runs_href",
    "runs_query",
    "trace_href",
]

MAX_MODELS_SHOWN = 2


class RunRow(BaseModel):
    """One table row (strings only: what the browser shows)."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    trace_id: str
    href: str
    start: str
    status: str
    name: str
    source: str
    duration: str
    models: str
    tokens: str
    tokens_note: str  # DD-012 footnote when usage comes from an agent span
    cost: str
    session: str
    generic: bool  # "generic" badge: no GenAI span (REQ-131)


def trace_href(prefix: str, project_id: str, trace_id: str) -> str:
    base = prefix.rstrip("/")
    return f"{base}/trace/{trace_id}?{urlencode({'project': project_id})}"


def _models(models: Sequence[str]) -> str:
    if not models:
        return DASH
    shown = ", ".join(models[:MAX_MODELS_SHOWN])
    extra = len(models) - MAX_MODELS_SHOWN
    return f"{shown} +{extra}" if extra > 0 else shown


def run_rows(runs: Iterable[RunSummary], prefix: str) -> list[RunRow]:
    rows = []
    for run in runs:
        name = run.agent_name or run.root_name or DASH
        rows.append(
            RunRow(
                project_id=run.project_id,
                trace_id=run.trace_id,
                href=trace_href(prefix, run.project_id, run.trace_id),
                start=fmt_time(run.start_ns),
                status=run.status,
                name=name,
                source=run.source.label if run.source else DASH,
                duration=fmt_duration(run.duration_ns),
                models=_models(run.models),
                tokens=fmt_int(run.total_tokens),
                tokens_note="from agent span" if run.usage_source == "agent" else "",
                cost=fmt_cost(run.cost_usd, run.unpriced),
                session=run.session_id or DASH,
                generic=not run.is_genai,
            )
        )
    return rows


# ── filter bar ───────────────────────────────────────────────────────────────────────────────


@dataclass
class FilterForm:
    """Every REQ-101 filter except Project, Source and time (shared chrome)."""

    text: str = ""
    statuses: list[str] = field(default_factory=list)
    genai: Literal["", "1", "0"] = ""
    models: str = ""  # comma-separated
    tools: str = ""  # comma-separated
    duration_ms: str = ""  # "100..", "..5000", "100..5000"
    tokens: str = ""
    cost_usd: str = ""
    include_unpriced: bool = False
    attributes: str = ""  # one ``key=value`` / ``key`` predicate per line
    session_id: str = ""
    sort: str = "start_desc"


def _range_text(rng: object) -> str:
    if rng is None:
        return ""
    lo, hi = getattr(rng, "min", None), getattr(rng, "max", None)

    def one(v: object) -> str:
        if v is None:
            return ""
        return format(v, "f") if isinstance(v, Decimal) else str(v)

    return f"{one(lo)}..{one(hi)}"


def form_from_query(q: RunsQuery) -> FilterForm:
    f = q.filter
    return FilterForm(
        text=f.text or "",
        statuses=list(f.statuses),
        genai="" if f.is_genai is None else ("1" if f.is_genai else "0"),
        models=", ".join(f.models),
        tools=", ".join(f.tools),
        duration_ms=_range_text(f.duration_ms),
        tokens=_range_text(f.tokens),
        cost_usd=_range_text(f.cost_usd),
        include_unpriced=f.include_unpriced_in_cost_range,
        attributes="\n".join(format_attr(a) for a in f.attributes),
        session_id=f.session_id or "",
        sort=q.sort,
    )


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def query_from_form(form: FilterForm, chrome: Chrome) -> str:
    """The Runs URL query for this form and chrome; ``ValidationError`` names a bad field."""
    pairs: list[tuple[str, str]] = [
        (k, v) for k, v in encode_chrome(chrome) if k in ("project", "source", "from", "to")
    ]
    if not any(k == "from" for k, _ in pairs):
        pairs.append(("from", chrome.from_))  # the chrome's default window, stated explicitly
    if form.text.strip():
        pairs.append(("q", form.text.strip()))
    pairs += [("status", s) for s in form.statuses]
    if form.genai:
        pairs.append(("genai", form.genai))
    pairs += [("model", m) for m in _split(form.models)]
    pairs += [("tool", t) for t in _split(form.tools)]
    for key, value in (("dur", form.duration_ms), ("tok", form.tokens), ("cost", form.cost_usd)):
        if value.strip():
            pairs.append((key, value.strip()))
    if form.include_unpriced:
        pairs.append(("unpriced", "1"))
    pairs += [("attr", line.strip()) for line in form.attributes.splitlines() if line.strip()]
    if form.session_id.strip():
        pairs.append(("session", form.session_id.strip()))
    if form.sort and form.sort != "start_desc":
        pairs.append(("sort", form.sort))
    parsed = parse_runs_query(pairs)  # one grammar: raises ValidationError(field)
    return runs_query_string(parsed.filter, parsed.sort)


def runs_query(params: str) -> RunsQuery:
    """The Runs URL → query; a malformed URL raises ``ValidationError`` (shown inline)."""
    return parse_runs_query(params)


def canonical(filt: RunFilter, sort: str = "start_desc") -> str:
    return runs_query_string(filt, sort)  # type: ignore[arg-type]


# ── live updates ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NoticeAction:
    kind: Literal["refresh", "count", "none"]
    new_runs: int = 0


def is_live_first_page(q: RunsQuery, live: bool) -> bool:
    """REQ-145.c: rows may move only on the live, first, newest-first page."""
    return live and q.cursor is None and q.sort == "start_desc"


def on_notice(
    notice: ChangeNotice, shown: Iterable[tuple[str, str]], *, live_first_page: bool
) -> NoticeAction:
    """What one coalesced notice does to the table.

    On the live first page any relevant change refreshes it (new Runs on top, in-progress rows
    updated). Elsewhere rows never shift (REQ-145.d): Runs not on screen are counted as new; a
    truncated notice means "at least one".
    """
    relevant = {"spans_committed", "runs_finalized", "purged"} & set(notice.kinds)
    if not relevant:
        return NoticeAction("none")
    if live_first_page:
        return NoticeAction("refresh")
    on_screen = set(shown)
    fresh = {(r.project_id, r.trace_id) for r in notice.run_keys} - on_screen
    if notice.truncated:
        return NoticeAction("count", max(1, len(fresh)))
    return NoticeAction("count", len(fresh)) if fresh else NoticeAction("none")


def run_key(row: RunRow) -> tuple[str, str]:
    return row.project_id, row.trace_id


def ref_key(ref: RunRef) -> tuple[str, str]:
    return ref.project_id, ref.trace_id


def runs_href(prefix: str, query: str) -> str:
    base = f"{prefix.rstrip('/')}/runs"
    return f"{base}?{query}" if query else base
