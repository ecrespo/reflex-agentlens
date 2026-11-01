"""Page-level helpers of the Runs and Trace pages (plan tasks P6-04.2, P6-05.2)."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qsl

from reflex_agentlens.query.dto import ChangeNotice, RunRef, TokenCostTotals
from reflex_agentlens.query.filters import parse_runs_query
from reflex_agentlens.views.chrome import Chrome
from reflex_agentlens.views.runs import query_from_form
from reflex_agentlens.views.trace import (
    TraceUrl,
    candidate_links,
    needs_reload,
    totals_rows,
    trace_page_href,
)

PID, OTHER = "01PROJECT0000000000000000A", "01PROJECT0000000000000000B"
TID = "ab" * 16


def test_submitted_filter_bar_becomes_the_url_req_101() -> None:
    from reflex_agentlens.pages.runs_page import form_from_data

    data = {
        "q": " refund ", "status_error": "on", "status_ok": True, "genai": "1",
        "models": "gpt-4o, claude", "dur": "100..", "unpriced": "on", "attrs": "user.id=42",
        "sort": "cost_desc", "evil": "<script>", "genai_bogus": "x",
    }  # fmt: skip
    query = query_from_form(form_from_data(data), Chrome(project=PID))
    pairs = parse_qsl(query)
    assert ("q", "refund") in pairs
    assert [v for k, v in pairs if k == "status"] == ["ok", "error"]
    assert [v for k, v in pairs if k == "model"] == ["gpt-4o", "claude"]
    assert ("sort", "cost_desc") in pairs
    assert ("project", PID) in pairs
    assert parse_runs_query(query).filter.include_unpriced_in_cost_range
    assert form_from_data({"genai": "maybe", "q": 3}).genai == ""


def test_trace_links_and_disambiguation_req_103b() -> None:
    url = TraceUrl(tab="cost", span="cd" * 8)
    assert trace_page_href("/obs/", TID, TraceUrl()) == f"/obs/trace/{TID}"
    links = candidate_links(
        [RunRef(project_id=PID, trace_id=TID), RunRef(project_id=OTHER, trace_id=TID)], "/o", url
    )
    assert [c["project"] for c in links] == [PID, OTHER]
    assert links[1]["href"] == f"/o/trace/{TID}?project={OTHER}&tab=cost&span={'cd' * 8}"


def test_cost_tab_totals() -> None:
    totals = TokenCostTotals(
        input_tokens=1000, output_tokens=20, cached_input_tokens=0, total_tokens=1020,
        cost_usd=Decimal("0.5"), includes_unpriced=True, unpriced_calls=1, usage_source="agent",
    )  # fmt: skip
    rows = {r["label"]: r["value"] for r in totals_rows(totals)}
    assert rows["Input tokens"] == "1,000"
    assert rows["Cost"] == "$0.5000 + unpriced"
    assert rows["Usage from"] == "agent span"


def _notice(*refs: RunRef, truncated: bool = False, kind: str = "spans_committed") -> ChangeNotice:
    return ChangeNotice(project_ids=[PID], run_keys=list(refs), truncated=truncated,
                        kinds=[kind])  # type: ignore[list-item]  # fmt: skip


def test_live_append_reloads_only_for_this_run_req_150() -> None:
    mine, other = RunRef(project_id=PID, trace_id=TID), RunRef(project_id=PID, trace_id="cd" * 16)
    assert needs_reload(_notice(mine), PID, TID)
    assert not needs_reload(_notice(other), PID, TID)
    assert needs_reload(_notice(truncated=True), PID, TID)
    assert not needs_reload(_notice(truncated=True), OTHER, TID)
    assert not needs_reload(_notice(mine, kind="settings_changed"), PID, TID)
