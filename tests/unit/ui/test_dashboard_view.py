"""Dashboard view model (plan task P6-09.1; REQ-106, 107, 130.d, 141, DD-017)."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

from reflex_agentlens.query.dto import (
    ApprovalDecisionCount,
    ApprovalStats,
    DashboardData,
    Kpis,
    LatencyRow,
    ModelUsage,
    ModelUsageRow,
    SeriesPoint,
    SourceRef,
    SourceUsage,
    TimeSeries,
    ToolStat,
)
from reflex_agentlens.views.chrome import Chrome
from reflex_agentlens.views.dashboard import (
    DAY,
    HOUR,
    MAX_DONUT,
    agg_scope,
    bucket_label,
    dashboard_charts,
    kpi_cards,
)
from tests.unit.ui.factories import T0

PID = "01PROJECT0000000000000000A"
CHROME = Chrome(project=PID)
B0 = T0 // HOUR * HOUR  # buckets start on the hour


def model_row(model: str, bucket: int | None, tokens: int, cost: str) -> ModelUsageRow:
    return ModelUsageRow(
        model=model,
        bucket_ns=bucket,
        calls=1,
        input_tokens=tokens,
        output_tokens=tokens // 10,
        cached_input_tokens=0,
        cost_usd=Decimal(cost),
        unpriced_calls=0,
        includes_unpriced=False,
    )


def source(name: str, cost: str) -> SourceUsage:
    return SourceUsage(source=SourceRef(service_name=name, service_namespace="shop"), runs=1,
                       input_tokens=1, output_tokens=1, cost_usd=Decimal(cost), unpriced_runs=0,
                       includes_unpriced=False)  # fmt: skip


def data(**kw: object) -> DashboardData:
    base: dict[str, object] = {
        "project_ids": [PID], "time_from_ns": T0, "time_to_ns": T0 + DAY, "bucket_ns": HOUR,
        "kpis": Kpis(runs=10, error_runs=2, error_rate=0.2, input_tokens=900, output_tokens=100,
                     total_tokens=1000, cost_usd=Decimal("1.5"), unpriced_runs=1,
                     includes_unpriced=True, p95_duration_ns=2_500_000_000, active_sessions=3),
        "runs_over_time": TimeSeries(bucket_ns=HOUR, groups=["error", "ok"], points=[
            SeriesPoint(bucket_ns=B0 + HOUR, group="ok", value=3),
            SeriesPoint(bucket_ns=B0, group="ok", value=5),
            SeriesPoint(bucket_ns=B0, group="error", value=2),
        ]),
        "model_usage": ModelUsage(bucket_ns=HOUR, totals=[
            model_row("small", None, 100, "0.01"), model_row("gpt-4o", None, 800, "1.2"),
        ], over_time=[model_row("gpt-4o", T0, 800, "1.2"), model_row("small", T0, 100, "0.01")]),
        "latency_by_operation": [LatencyRow(group="chat", count=4, p50_ns=120_000_000,
                                            p95_ns=900_000_000, p99_ns=1_000_000_000)],
        "tools": [ToolStat(tool_name="search", calls=4, successes=3, errors=1, error_rate=0.25),
                  ToolStat(tool_name="refund", calls=9, successes=9, errors=0, error_rate=0.0)],
        "by_source": [source("api", "1.0"), source("worker", "0.5")],
        "approvals": ApprovalStats(by_decision=[
            ApprovalDecisionCount(decision="approved", count=3, median_wait_ns=4_000_000_000),
            ApprovalDecisionCount(decision="rejected", count=0, median_wait_ns=None),
        ], median_wait_ns=4_000_000_000),
    }  # fmt: skip
    base.update(kw)
    return DashboardData(**base)  # type: ignore[arg-type]


def test_scope_and_bucket_labels() -> None:
    scope = agg_scope(Chrome(project=PID, sources=("shop/api",), range="7d", from_="now-7d"))
    assert (scope.project_ids, scope.sources, scope.time_from, scope.time_to) == (
        [PID], ["shop/api"], "now-7d", "now",
    )  # fmt: skip
    assert bucket_label(T0, DAY) == "11-01"
    assert bucket_label(T0, HOUR) == "11-01 02:00"
    assert bucket_label(T0, 60 * 10**9) == "02:26"


def test_kpi_cards_req_106g() -> None:
    cards = {c["title"]: c for c in kpi_cards(data().kpis)}
    assert list(cards) == ["Runs", "Error rate", "Total tokens", "Total cost", "p95 Run duration",
                           "Active sessions"]  # fmt: skip
    assert cards["Error rate"]["value"] == "20.0%"
    assert cards["Total cost"]["value"] == "$1.50"
    assert cards["Total cost"]["footer"] == "1 unpriced runs"
    assert cards["p95 Run duration"]["value"] == "2.50 s"
    assert kpi_cards(None) == []


def test_series_shaping_req_141b() -> None:
    charts, _ = dashboard_charts(data(), "/obs", CHROME)
    assert charts.status_keys == ["ok", "error"]  # known statuses first
    assert [(r["x"], r["ok"], r["error"]) for r in charts.runs_by_status] == [
        ("11-01 02:00", 5, 2), ("11-01 03:00", 3, 0),
    ]  # fmt: skip
    assert [r["x"] for r in charts.tokens_by_model] == ["gpt-4o", "small"]  # by volume
    assert charts.tokens_by_model[0]["output"] == 80
    assert charts.cost_by_model[0]["value"] == 1.2
    assert charts.model_keys == ["gpt-4o", "small"]
    assert charts.tokens_over_time == [{"x": "11-01 02:00", "gpt-4o": 880, "small": 110}]
    assert charts.latency_by_operation == [{"label": "chat", "p50": 120.0, "p95": 900.0}]
    assert [r["x"] for r in charts.top_tools] == ["refund", "search"]
    assert charts.tool_outcomes[0] == {"x": "search", "success": 3, "error": 1,
                                       "link": charts.tool_outcomes[0]["link"]}  # fmt: skip
    assert charts.approvals == [{"name": "approved", "value": 3}]
    assert charts.approval_wait == "4.00 s"
    assert charts.source_donut


def test_many_sources_group_into_other_dd017() -> None:
    many = [source(f"s{i}", str(i)) for i in range(MAX_DONUT + 3)]
    charts, _ = dashboard_charts(data(by_source=many), "/obs", CHROME)
    assert not charts.source_donut
    assert len(charts.cost_by_source) == MAX_DONUT
    assert charts.cost_by_source[-1]["name"] == "other"
    assert charts.cost_by_source[-1]["value"] == 3 + 2 + 1 + 0  # the four cheapest


def query_of(url: str | None) -> dict[str, list[str]]:
    assert url is not None
    out: dict[str, list[str]] = {}
    for k, v in parse_qsl(urlsplit(url).query):
        out.setdefault(k, []).append(v)
    return out


def test_click_to_filter_req_141c() -> None:
    charts, links = dashboard_charts(data(), "/obs", CHROME)
    model = charts.tokens_by_model[0]
    url = links.resolve("model", {"seriesKey": "input", "datum": model})
    assert url is not None
    assert url.startswith("/obs/runs?")
    assert query_of(url)["model"] == ["gpt-4o"]
    assert query_of(url)["project"] == [PID]
    tool = links.resolve("tool", {"datum": charts.top_tools[0]})
    assert query_of(tool)["tool"] == ["refund"]
    bucket = links.resolve("runs", {"datum": charts.runs_by_status[0]})
    q = query_of(bucket)
    assert (q["from"], q["to"]) == (["2026-11-01T02:00:00Z"], ["2026-11-01T03:00:00Z"])
    src = links.resolve("source", {"datum": charts.cost_by_source[0]})
    assert query_of(src)["source"] == ["shop/api"]
    # anything else the browser sends resolves to nothing: no open redirect
    for junk in (None, "x", {"datum": {"link": "https://evil.example"}}, {"datum": "x"},
                 {"datum": {"link": 3}}):  # fmt: skip
        assert links.resolve("model", junk) is None
    assert links.resolve("tool", {"datum": model}) is None  # a model key is not a tool key


def test_failed_panels_req_130d() -> None:
    charts, _ = dashboard_charts(
        data(kpis=None, tools=None, failed=["tools", "kpis", "bogus"]), "/obs", CHROME
    )
    assert charts.failed == ["tools", "kpis"]
    assert charts.top_tools == []
