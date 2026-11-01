"""Sessions view model (plan task P6-08.1; REQ-105, 160)."""

from __future__ import annotations

from decimal import Decimal

from reflex_agentlens.query.dto import SessionDetail, SessionRunView, SessionSummary
from reflex_agentlens.views.sessions import (
    cumulative_series,
    session_href,
    session_rows,
    session_run_rows,
    sessions_href,
    sessions_query,
)
from tests.unit.ui.factories import MS, T0, run

SID = "01JB0000000000000000000000"


def summary(**kw: object) -> SessionSummary:
    base: dict[str, object] = {
        "project_id": "01PROJECT0000000000000000A", "session_id": SID, "external_id": "chat-42",
        "first_ns": T0, "last_ns": T0 + 125_000 * MS, "run_count": 3, "input_tokens": 900,
        "output_tokens": 100, "cached_input_tokens": 0, "total_tokens": 1000,
        "cost_usd": Decimal("0.25"), "includes_unpriced": True,
    }  # fmt: skip
    base.update(kw)
    return SessionSummary(**base)  # type: ignore[arg-type]


def test_list_rows_req_160a() -> None:
    (row,) = session_rows([summary()], "/obs")
    assert row.href == f"/obs/sessions/{SID}"
    assert (row.external_id, row.runs, row.tokens) == ("chat-42", "3", "1,000")
    assert row.span == "2 m 05 s"
    assert row.cost == "$0.2500 + unpriced"
    assert session_rows([summary(external_id="")], "/obs")[0].external_id == "—"


def test_query_and_links_never_raise() -> None:
    q = sessions_query({"project": "shop", "from": "now-7d", "sort": "cost_desc", "cursor": "abc"})
    assert q.filter.project_ids == ["shop"]
    assert q.filter.time_from == "now-7d"
    assert (q.sort, q.cursor) == ("cost_desc", "abc")
    assert sessions_href("/obs", q, cursor="nxt") == (
        "/obs/sessions?project=shop&from=now-7d&sort=cost_desc&cursor=nxt"
    )
    bad = sessions_query({"sort": "drop table", "cursor": "\x00"})
    assert (bad.sort, bad.cursor) == ("last_desc", None)
    assert sessions_href("/obs", bad) == "/obs/sessions"
    assert session_href("/obs/", "a/b") == "/obs/sessions/a%2Fb"


def detail() -> SessionDetail:
    views = [
        SessionRunView(run=run(trace_id=f"{i:032x}", total_tokens=100 * i,
                               cost_usd=Decimal("0.1") * i),
                       cumulative_tokens=sum(100 * k for k in range(1, i + 1)),
                       cumulative_cost_usd=sum((Decimal("0.1") * k for k in range(1, i + 1)),
                                               Decimal(0)),
                       cumulative_includes_unpriced=i == 3)
        for i in (1, 2, 3)
    ]  # fmt: skip
    return SessionDetail(session=summary(), runs=views)


def test_cumulative_series_req_160c() -> None:
    series = cumulative_series(detail())
    assert [p["x"] for p in series] == ["#1", "#2", "#3"]
    assert [p["tokens"] for p in series] == [100, 300, 600]
    assert [p["cost"] for p in series] == [0.1, 0.3, 0.6]


def test_runs_in_order_with_cumulative_columns_req_160b() -> None:
    rows = session_run_rows(detail(), "/obs")
    assert [r.index for r in rows] == ["1", "2", "3"]
    assert rows[2].cumulative_tokens == "600"
    assert rows[2].cumulative_cost == "$0.6000 + unpriced"
    assert rows[0].href.startswith("/obs/trace/")
