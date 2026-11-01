"""Dashboard view model (plan task P6-09.1; REQ-106, 107, 130.c, 141, DD-017).

``DashboardData`` → KPI cards and one row list per silverpoint chart of the DD-017 mapping.
Every chart row carries a ``link`` key; :class:`Links` maps ``(chart, link)`` back to a Runs URL
on the server, so a click never sends a URL from the browser (REQ-141.c). A panel listed in
``DashboardData.failed`` shows its own error state (REQ-130.d); a missing one shows "no data".

Chart values are numbers (``float`` for cost) because the charts draw them; labels and KPI
figures are formatted from the ``Decimal`` values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from reflex_agentlens.query.dto import (
    AggScope,
    ApprovalStats,
    DashboardData,
    Kpis,
    LatencyRow,
    ModelUsage,
    SourceUsage,
    TimeSeries,
    ToolStat,
)
from reflex_agentlens.views.chrome import Chrome
from reflex_agentlens.views.fmt import DASH, fmt_cost, fmt_duration, fmt_int
from reflex_agentlens.views.runs import FilterForm, query_from_form, runs_href

__all__ = [
    "MAX_DONUT",
    "PANELS",
    "Charts",
    "Links",
    "agg_scope",
    "bucket_label",
    "dashboard_charts",
    "kpi_cards",
]

MAX_DONUT = 6  # DD-017: a donut for ≤ 6 Sources, the rest grouped as "other"
MINUTE, HOUR, DAY = 60 * 10**9, 3600 * 10**9, 86400 * 10**9
PANELS: tuple[str, ...] = (
    "kpis", "runs_over_time", "model_usage", "latency_by_operation", "latency_by_node",
    "tools", "by_source", "approvals", "metric_tokens",
)  # fmt: skip
RUN_STATUSES = ("ok", "error", "in_progress", "incomplete")
Row = dict[str, Any]


def agg_scope(chrome: Chrome) -> AggScope:
    return AggScope(
        project_ids=[chrome.project] if chrome.project else [],
        sources=list(chrome.sources),
        time_from=chrome.from_,
        time_to=chrome.to or "now",
    )


def bucket_label(bucket_start_ns: int, bucket_ns: int) -> str:
    """Pre-formatted time axis labels (DD-017 gap #3), UTC."""
    when = datetime.fromtimestamp(bucket_start_ns / 1e9, UTC)
    if bucket_ns >= DAY:
        return when.strftime("%m-%d")
    if bucket_ns >= HOUR:
        return when.strftime("%m-%d %H:00")
    return when.strftime("%H:%M")


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── KPI cards (REQ-106.g, 141.a) ─────────────────────────────────────────────────────────────


def kpi_cards(kpis: Kpis | None) -> list[dict[str, str]]:
    if kpis is None:
        return []
    cost = fmt_cost(kpis.cost_usd, kpis.includes_unpriced and kpis.cost_usd == 0)
    unpriced = f"{fmt_int(kpis.unpriced_runs)} unpriced runs" if kpis.unpriced_runs else ""
    return [
        {"title": "Runs", "value": fmt_int(kpis.runs), "footer": ""},
        {"title": "Error rate", "value": f"{kpis.error_rate * 100:.1f}%",
         "footer": f"{fmt_int(kpis.error_runs)} errors"},
        {"title": "Total tokens", "value": fmt_int(kpis.total_tokens),
         "footer": f"{fmt_int(kpis.input_tokens)} in / {fmt_int(kpis.output_tokens)} out"},
        {"title": "Total cost", "value": cost, "footer": unpriced},
        {"title": "p95 Run duration", "value": fmt_duration(kpis.p95_duration_ns), "footer": ""},
        {"title": "Active sessions", "value": fmt_int(kpis.active_sessions), "footer": ""},
    ]  # fmt: skip


# ── click-to-filter links (REQ-141.c) ────────────────────────────────────────────────────────


@dataclass
class Links:
    """Server-side ``(chart, link) → Runs URL``: the browser only sends the key back."""

    prefix: str
    chrome: Chrome
    urls: dict[str, str] = field(default_factory=dict)

    def add(self, chart: str, form: FilterForm, chrome: Chrome | None = None) -> str:
        link = f"{len(self.urls)}"
        self.urls[f"{chart}:{link}"] = runs_href(
            self.prefix, query_from_form(form, chrome or self.chrome)
        )
        return link

    def resolve(self, chart: str, item: object) -> str | None:
        """The URL for a chart's ``on_select`` payload (``{datum: {link}}``), or ``None``."""
        if not isinstance(item, dict):
            return None
        datum = item.get("datum")
        link = datum.get("link") if isinstance(datum, dict) else None
        return self.urls.get(f"{chart}:{link}") if isinstance(link, str) else None


def _bucket_chrome(chrome: Chrome, start_ns: int, bucket_ns: int) -> Chrome:
    return Chrome(chrome.project, chrome.sources, "custom", _iso(start_ns),
                  _iso(start_ns + bucket_ns), chrome.live)  # fmt: skip


# ── charts (REQ-141.b, DD-017) ───────────────────────────────────────────────────────────────


@dataclass
class Charts:
    runs_by_status: list[Row] = field(default_factory=list)
    status_keys: list[str] = field(default_factory=list)
    tokens_by_model: list[Row] = field(default_factory=list)
    cost_by_model: list[Row] = field(default_factory=list)
    tokens_over_time: list[Row] = field(default_factory=list)
    model_keys: list[str] = field(default_factory=list)
    latency_by_operation: list[Row] = field(default_factory=list)
    latency_by_node: list[Row] = field(default_factory=list)
    tool_outcomes: list[Row] = field(default_factory=list)
    top_tools: list[Row] = field(default_factory=list)
    cost_by_source: list[Row] = field(default_factory=list)
    source_donut: bool = True
    approvals: list[Row] = field(default_factory=list)
    approval_wait: str = ""
    metric_tokens: list[Row] = field(default_factory=list)
    metric_keys: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _series_rows(ts: TimeSeries | None) -> tuple[list[Row], list[str]]:
    if ts is None:
        return [], []
    by_bucket: dict[int, Row] = {}
    for p in sorted(ts.points, key=lambda p: p.bucket_ns):
        row = by_bucket.setdefault(p.bucket_ns, {"x": bucket_label(p.bucket_ns, ts.bucket_ns)})
        row[p.group] = row.get(p.group, 0) + p.value
    keys = list(ts.groups)
    rows = [{**dict.fromkeys(keys, 0), **row} for row in by_bucket.values()]
    return rows, keys


def _runs_by_status(ts: TimeSeries | None, links: Links) -> tuple[list[Row], list[str]]:
    rows, keys = _series_rows(ts)
    if ts is None:
        return rows, keys
    starts = sorted({p.bucket_ns for p in ts.points})
    for row, start in zip(rows, starts, strict=True):
        row["link"] = links.add("runs", FilterForm(),
                                _bucket_chrome(links.chrome, start, ts.bucket_ns))  # fmt: skip
    ordered = [s for s in RUN_STATUSES if s in keys] + [k for k in keys if k not in RUN_STATUSES]
    return rows, ordered


def _models(mu: ModelUsage | None, links: Links) -> tuple[list[Row], list[Row], list[Row],
                                                           list[str]]:  # fmt: skip
    if mu is None:
        return [], [], [], []
    tokens: list[Row] = []
    cost: list[Row] = []
    for usage in sorted(mu.totals, key=lambda r: (-r.input_tokens - r.output_tokens, r.model)):
        link = links.add("model", FilterForm(models=usage.model))
        tokens.append({"x": usage.model, "input": usage.input_tokens,
                       "output": usage.output_tokens, "cached": usage.cached_input_tokens,
                       "link": link})  # fmt: skip
        cost.append({"x": usage.model, "value": float(usage.cost_usd), "link": link,
                     "label": fmt_cost(usage.cost_usd, usage.includes_unpriced)})  # fmt: skip
    models = [str(r["x"]) for r in tokens]
    over: dict[int, Row] = {}
    for r in sorted(mu.over_time, key=lambda r: r.bucket_ns or 0):
        b = r.bucket_ns or 0
        row = over.setdefault(b, {"x": bucket_label(b, mu.bucket_ns), **dict.fromkeys(models, 0)})
        row[r.model] = row.get(r.model, 0) + r.input_tokens + r.output_tokens
    return tokens, cost, list(over.values()), models


def _latency(rows: Sequence[LatencyRow] | None) -> list[Row]:
    return [
        {"label": r.group or DASH, "p50": round(r.p50_ns / 1e6, 1), "p95": round(r.p95_ns / 1e6, 1)}
        for r in rows or ()
    ]


def _tools(tools: Sequence[ToolStat] | None, links: Links) -> tuple[list[Row], list[Row]]:
    outcomes: list[Row] = []
    top: list[Row] = []
    for t in tools or ():
        link = links.add("tool", FilterForm(tools=t.tool_name))
        outcomes.append({"x": t.tool_name, "success": t.successes, "error": t.errors,
                         "link": link})  # fmt: skip
        top.append({"x": t.tool_name, "value": t.calls, "link": link})
    top.sort(key=lambda r: (-r["value"], r["x"]))
    return outcomes, top


def _sources(rows: Sequence[SourceUsage] | None, links: Links) -> tuple[list[Row], bool]:
    items = sorted(rows or (), key=lambda r: (-r.cost_usd, r.source.label))
    if len(items) <= MAX_DONUT:
        out = []
        for r in items:
            chrome = Chrome(links.chrome.project, (r.source.label,), links.chrome.range,
                            links.chrome.from_, links.chrome.to, links.chrome.live)  # fmt: skip
            out.append({"name": r.source.label, "x": r.source.label,
                        "value": float(r.cost_usd),
                        "link": links.add("source", FilterForm(), chrome)})  # fmt: skip
        return out, True
    head, rest = items[: MAX_DONUT - 1], items[MAX_DONUT - 1 :]
    other = sum((r.cost_usd for r in rest), Decimal(0))
    out = [{"name": r.source.label, "x": r.source.label, "value": float(r.cost_usd)}
           for r in head]  # fmt: skip
    out.append({"name": "other", "x": "other", "value": float(other)})
    return out, False


def _approvals(stats: ApprovalStats | None) -> tuple[list[Row], str]:
    if stats is None:
        return [], ""
    rows = [{"name": d.decision, "value": d.count} for d in stats.by_decision if d.count]
    wait = fmt_duration(stats.median_wait_ns) if stats.median_wait_ns is not None else ""
    return rows, wait


def dashboard_charts(data: DashboardData, prefix: str, chrome: Chrome) -> tuple[Charts, Links]:
    links = Links(prefix, chrome)
    charts = Charts(failed=[p for p in data.failed if p in PANELS])
    charts.runs_by_status, charts.status_keys = _runs_by_status(data.runs_over_time, links)
    (charts.tokens_by_model, charts.cost_by_model, charts.tokens_over_time,
     charts.model_keys) = _models(data.model_usage, links)  # fmt: skip
    charts.latency_by_operation = _latency(data.latency_by_operation)
    charts.latency_by_node = _latency(data.latency_by_node)
    charts.tool_outcomes, charts.top_tools = _tools(data.tools, links)
    charts.cost_by_source, charts.source_donut = _sources(data.by_source, links)
    charts.approvals, charts.approval_wait = _approvals(data.approvals)
    charts.metric_tokens, charts.metric_keys = _series_rows(data.metric_tokens)
    return charts, links
