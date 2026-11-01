"""Waterfall and trace URL view model (plan tasks P6-05.1, P6-05.2; REQ-037.b, 131, 150)."""

from __future__ import annotations

from decimal import Decimal

from reflex_agentlens.views.tokens import KIND_COLORS
from reflex_agentlens.views.trace import (
    TraceUrl,
    decode_trace_url,
    encode_trace_url,
    tabs_for,
    toggle,
    waterfall_rows,
)
from tests.unit.ui.factories import run, span

#   r (0-1000) ─ a (100-600) ─ b (200-300) ─ c (210-250) ─ d (220-230)
#              └ e (700-1000, error, llm)
TREE = [
    span("r", None, 0, 0, 1000, kind="agent", is_genai=True, subtree_tokens=120,
         subtree_cost_usd=Decimal("0.01")),
    span("a", "r", 1, 100, 600),
    span("b", "a", 2, 200, 300),
    span("c", "b", 3, 210, 250),
    span("d", "c", 4, 220, 230),
    span("e", "r", 1, 700, 1000, kind="llm", is_genai=True, status="error", redacted=True),
]  # fmt: skip


def ids(rows: list) -> list[str]:
    return [r.span_id[0] for r in rows]


def test_bar_geometry_req_150() -> None:
    rows = {r.span_id[0]: r for r in waterfall_rows(run(), TREE)}
    assert (rows["r"].offset_pct, rows["r"].width_pct) == (0.0, 100.0)
    assert (rows["a"].offset_pct, rows["a"].width_pct) == (10.0, 50.0)
    assert (rows["e"].offset_pct, rows["e"].width_pct) == (70.0, 30.0)
    assert rows["e"].error
    assert rows["e"].color == KIND_COLORS["llm"]
    assert rows["e"].badges == "redacted"
    assert rows["a"].indent_px == 16
    assert rows["r"].tokens == "120"
    assert rows["r"].cost == "$0.0100"


def test_zero_length_spans_stay_visible() -> None:
    (row,) = waterfall_rows(run(), [span("z", None, 0, 500, 500)])
    assert row.width_pct == 0.5


def test_collapse_beyond_depth_3_and_toggle_req_150() -> None:
    rows = waterfall_rows(run(), TREE)
    assert ids(rows) == ["r", "a", "b", "c", "e"]  # c (depth 3) starts collapsed: d hidden
    assert next(r for r in rows if r.span_id[0] == "c").collapsed
    c_id = TREE[3].span_id
    expanded, collapsed = toggle(c_id, 3, frozenset(), frozenset(), currently_collapsed=True)
    assert ids(waterfall_rows(run(), TREE, expanded=expanded, collapsed=collapsed)) == list(
        "rabcde"
    )
    a_id = TREE[1].span_id
    expanded, collapsed = toggle(a_id, 1, expanded, collapsed, currently_collapsed=False)
    assert ids(waterfall_rows(run(), TREE, expanded=expanded, collapsed=collapsed)) == [
        "r",
        "a",
        "e",
    ]


def test_placeholder_rows_for_missing_parents_req_037b() -> None:
    spans = [
        span("p", None, 0, 0, 0, placeholder=True, name="", label=""),
        span("x", "p", 1, 100, 200, missing_parent=True),
    ]
    placeholder, child = waterfall_rows(run(), spans)
    assert placeholder.placeholder
    assert (placeholder.offset_pct, placeholder.width_pct, placeholder.duration) == (
        0.0,
        100.0,
        "—",
    )
    assert "missing parent" in child.badges


def test_url_state_never_raises_api_7_2() -> None:
    url = decode_trace_url({"tab": "graph", "span": "AB" * 8, "replay": "cd" * 8, "speed": "5"})
    assert url == TraceUrl(tab="graph", span="ab" * 8, replay="cd" * 8, speed="5")
    assert encode_trace_url(url) == [
        ("tab", "graph"), ("span", "ab" * 8), ("replay", "cd" * 8), ("speed", "5"),
    ]  # fmt: skip
    junk = decode_trace_url({"tab": "evil", "span": "<script>", "speed": "99", "project": "\x00"})
    assert junk == TraceUrl()
    assert encode_trace_url(TraceUrl()) == []


def test_generic_traces_have_no_genai_tabs_req_131() -> None:
    assert tabs_for(run()) == ["waterfall", "graph", "cost", "raw"]
    assert tabs_for(run(is_genai=False)) == ["waterfall", "raw"]
