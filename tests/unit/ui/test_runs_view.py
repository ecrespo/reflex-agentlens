"""Runs page view model (plan tasks P6-04.1, P6-04.2; REQ-101, 102, 131, 145.a/c/d)."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qsl

import pytest

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.query.dto import ChangeNotice, RunRef
from reflex_agentlens.query.filters import parse_runs_query
from reflex_agentlens.views.chrome import Chrome
from reflex_agentlens.views.fmt import fmt_cost, fmt_duration
from reflex_agentlens.views.runs import (
    FilterForm,
    form_from_query,
    is_live_first_page,
    on_notice,
    query_from_form,
    run_rows,
)
from tests.unit.ui.factories import MS, run

PID = "01PROJECT0000000000000000A"


def test_columns_req_145a() -> None:
    (row,) = run_rows([run()], "/agentlens")
    assert row.start == "2026-11-01 02:26:40"
    assert (row.status, row.name, row.source) == ("ok", "support", "shop/api")
    assert (row.duration, row.models, row.tokens, row.cost) == (
        "1.00 s",
        "gpt-4o",
        "120",
        "$0.0123",
    )
    assert row.href == f"/agentlens/trace/{'ab' * 16}?project={PID}"
    assert not row.generic
    assert row.session == "—"


def test_generic_badge_footnote_and_unpriced_req_131() -> None:
    generic, agent_usage, unpriced, running = run_rows(
        [
            run(is_genai=False, agent_name=None, models=[], cost_usd=None),
            run(usage_source="agent"),
            run(cost_usd=None, unpriced=True),
            run(status="in_progress", end_ns=None, duration_ns=None, models=["a", "b", "c", "d"]),
        ],
        "/x/",
    )
    assert generic.generic
    assert generic.name == "invoke_agent support"
    assert generic.cost == "—"
    assert agent_usage.tokens_note == "from agent span"  # DD-012
    assert unpriced.cost == "unpriced"
    assert running.duration == "—"
    assert running.models == "a, b +2"
    assert running.href.startswith("/x/trace/")


def test_formatting() -> None:
    assert fmt_duration(850 * MS) == "850 ms"
    assert fmt_duration(125_000 * MS) == "2 m 05 s"
    assert fmt_duration(3_720_000 * MS) == "1 h 02 m"
    assert fmt_cost(Decimal("12.5")) == "$12.50"
    assert fmt_cost(Decimal("0.5"), unpriced=True) == "$0.5000 + unpriced"


FULL = (
    "project=" + PID + "&source=shop%2Fapi&from=now-7d&status=error&status=ok&genai=1"
    "&model=gpt-4o&tool=search&dur=100..5000&tok=..900&cost=0.01..&unpriced=1"
    "&attr=user.id%3D42&q=refund&session=01JB0000000000000000000000&sort=duration_desc"
)


def test_every_filter_round_trips_through_the_form_req_101_145b() -> None:
    q = parse_runs_query(FULL)
    form = form_from_query(q)
    chrome = Chrome(project=PID, sources=("shop/api",), range="7d", from_="now-7d")
    again = query_from_form(form, chrome)
    assert parse_runs_query(again) == q
    assert form.models == "gpt-4o"
    assert form.attributes == "root:user.id=42"  # canonical form names the scope


def test_the_chrome_window_is_always_explicit() -> None:
    assert dict(parse_qsl(query_from_form(FilterForm(), Chrome()))) == {"from": "now-24h"}


@pytest.mark.parametrize(
    ("field", "value", "key"),
    [("duration_ms", "5..1", "dur"), ("tokens", "abc", "tok"), ("session_id", "x", "session"),
     ("attributes", "bad key!", "attr")],
)  # fmt: skip
def test_invalid_form_values_name_their_field(field: str, value: str, key: str) -> None:
    form = FilterForm(**{field: value})  # type: ignore[arg-type]
    with pytest.raises(ValidationError) as info:
        query_from_form(form, Chrome())
    assert info.value.field == key


def notice(
    *keys: str, truncated: bool = False, kinds: tuple[str, ...] = ("spans_committed",)
) -> ChangeNotice:
    refs = [RunRef(project_id=PID, trace_id=k * 32) for k in keys]
    return ChangeNotice(project_ids=[PID], run_keys=refs, truncated=truncated, kinds=list(kinds))  # type: ignore[arg-type]


def test_live_first_page_refreshes_req_145c() -> None:
    q = parse_runs_query("")
    assert is_live_first_page(q, live=True)
    assert not is_live_first_page(q, live=False)
    assert not is_live_first_page(parse_runs_query("sort=cost_desc"), live=True)
    assert on_notice(notice("a"), [], live_first_page=True).kind == "refresh"


def test_other_pages_count_without_shifting_req_145d() -> None:
    shown = [(PID, "a" * 32)]
    assert on_notice(notice("a"), shown, live_first_page=False).kind == "none"  # update only
    action = on_notice(notice("a", "b", "c"), shown, live_first_page=False)
    assert (action.kind, action.new_runs) == ("count", 2)
    assert on_notice(notice(truncated=True), shown, live_first_page=False).new_runs == 1
    assert (
        on_notice(notice("b", kinds=("settings_changed",)), shown, live_first_page=False).kind
        == "none"
    )
