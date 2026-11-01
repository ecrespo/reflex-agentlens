"""Span tree, totals and drawer helpers (plan task P4-02.2; REQ-103.a, 037.b, 151.c/d, 152.a)."""

from __future__ import annotations

import random
from decimal import Decimal

from reflex_agentlens.query.tree import (
    PLACEHOLDER_NAME,
    build_tree,
    error_info,
    llm_messages,
    page,
    tool_io,
)
from reflex_agentlens.store import records as r

SRC = r.SourceKey("svc")


def sid(n: int) -> bytes:
    return n.to_bytes(8, "big")


def span(
    n: int,
    parent: int | None,
    start: int,
    *,
    llm: r.LLMCallRow | None = None,
    category: str = "generic",
    flags: int = 0,
    status: int = 0,
) -> r.TraceSpan:
    return r.TraceSpan(
        span_id=sid(n),
        parent_span_id=None if parent is None else sid(parent),
        name=f"s{n}",
        category=category,  # type: ignore[arg-type]
        otel_kind=0,
        start_ns=start,
        end_ns=start + 10,
        status_code=status,
        status_message=None,
        flags=flags,
        source=SRC,
        agent_name=None,
        llm_call=llm,
        tool_call=None,
        approval=None,
        graph_node=None,
    )


def call(tokens: int, cost: int | None) -> r.LLMCallRow:
    return r.LLMCallRow(
        start_ns=0, duration_ns=1, operation="chat", unpriced=cost is None, model="m",
        input_tokens=tokens, output_tokens=0, cost_nusd=cost,
    )  # fmt: skip


def ids(spans: list[r.TraceSpan]) -> list[tuple[str, int]]:
    return [(n.span_id, n.depth) for n in build_tree(spans).nodes]


def test_dfs_order_siblings_by_start_req_103a() -> None:
    spans = [span(1, None, 0), span(2, 1, 20), span(3, 1, 10), span(4, 3, 15), span(5, None, 5)]
    assert ids(spans) == [
        (sid(1).hex(), 0),
        (sid(3).hex(), 1),
        (sid(4).hex(), 2),
        (sid(2).hex(), 1),
        (sid(5).hex(), 0),
    ]


def test_tree_is_independent_of_arrival_order_req_037b() -> None:
    spans = [span(i, None if i == 1 else (i // 2), i * 3) for i in range(1, 60)]
    expected = build_tree(spans).nodes
    rng = random.Random(4)  # noqa: S311 - deterministic shuffle
    for _ in range(5):
        shuffled = spans[:]
        rng.shuffle(shuffled)
        assert build_tree(shuffled).nodes == expected


def test_orphans_sit_under_one_placeholder_per_missing_parent_req_037b() -> None:
    spans = [span(1, None, 0), span(2, 99, 5), span(3, 99, 3), span(4, 98, 8)]
    nodes = build_tree(spans).nodes
    assert [(n.span_id, n.depth, n.placeholder, n.missing_parent) for n in nodes] == [
        (sid(1).hex(), 0, False, False),
        (sid(99).hex(), 0, True, False),
        (sid(3).hex(), 1, False, True),
        (sid(2).hex(), 1, False, True),
        (sid(98).hex(), 0, True, False),
        (sid(4).hex(), 1, False, True),
    ]
    holder = nodes[1]
    assert holder.name == PLACEHOLDER_NAME
    assert (holder.start_ns, holder.end_ns) == (3, 15)


def test_parent_cycles_still_list_every_span_once() -> None:
    spans = [span(1, 2, 0), span(2, 1, 5), span(3, None, 1)]
    nodes = build_tree(spans).nodes
    assert sorted(n.span_id for n in nodes) == sorted(sid(i).hex() for i in (1, 2, 3))
    assert nodes[1].span_id == sid(1).hex()
    assert nodes[1].missing_parent


def test_subtree_totals_req_152a() -> None:
    spans = [
        span(1, None, 0),
        span(2, 1, 1, llm=call(100, 1_000), category="llm"),
        span(3, 1, 2),
        span(4, 3, 3, llm=call(50, None), category="llm"),
        span(5, 77, 4, llm=call(7, 3)),
    ]
    by_id = {n.span_id: n for n in build_tree(spans).nodes}
    root = by_id[sid(1).hex()]
    assert root.subtree_tokens == 150
    assert root.subtree_cost_usd == Decimal("0.000001")
    assert root.subtree_includes_unpriced
    assert by_id[sid(2).hex()].subtree_includes_unpriced is False
    assert by_id[sid(77).hex()].subtree_tokens == 7  # placeholder sums its orphans
    assert by_id[sid(2).hex()].label == "chat m · 100 tok"
    tree = build_tree(spans)
    assert [c.span_id for c in tree.llm_calls] == [sid(i).hex() for i in (2, 4, 5)]
    assert all(c.messages == [] for c in tree.llm_calls)


def test_flags_kinds_and_status() -> None:
    node = build_tree([span(1, None, 0, flags=1 | 2 | 4, category="generic_genai", status=2)])
    n = node.nodes[0]
    assert (n.truncated, n.redacted, n.content_dropped) == (True, True, True)
    assert (n.kind, n.is_genai, n.status) == ("generic", True, "error")


def test_deep_chain_does_not_recurse() -> None:
    spans = [span(i, None if i == 1 else i - 1, i) for i in range(1, 5001)]
    nodes = build_tree(spans).nodes
    assert nodes[-1].depth == 4999


def test_paging() -> None:
    nodes = build_tree([span(i, None, i) for i in range(1, 6)]).nodes
    assert [len(page(nodes, 0, 2)[0]), page(nodes, 0, 2)[1]] == [2, 2]
    assert page(nodes, 4, 2)[1] is None
    assert page(nodes, 10, 2) == ([], None)


def _raw(attributes: dict[str, object], events: list[object]) -> r.SpanRaw:
    return r.SpanRaw(sid(1), attributes, {}, None, None, events, [], [])  # type: ignore[arg-type]


def test_tool_io_req_151c() -> None:
    raw = _raw({"gen_ai.tool.call.arguments": {"q": "x"}, "gen_ai.tool.call.result": "ok"}, [])
    assert tool_io(raw) == ({"q": "x"}, "ok")
    assert tool_io(_raw({}, [])) == (None, None)


def test_error_info_req_151d() -> None:
    exc = {
        "name": "exception",
        "time_ns": 1,
        "attributes": {
            "exception.type": "ValueError",
            "exception.message": "bad",
            "exception.stacktrace": "Traceback ...",
        },
    }
    info = error_info(2, None, _raw({}, [exc]))
    assert info is not None
    assert (info.message, info.exception_type, info.stacktrace) == (
        "bad",
        "ValueError",
        "Traceback ...",
    )
    status_only = error_info(2, "timeout", _raw({"error.type": "Timeout"}, []))
    assert status_only is not None
    assert (status_only.message, status_only.exception_type) == ("timeout", "Timeout")
    assert error_info(1, None, _raw({}, [])) is None


def test_llm_messages_from_attributes_req_104() -> None:
    attrs = {
        "gen_ai.input.messages": [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}],
        "gen_ai.output.messages": [
            {"role": "assistant", "parts": [{"type": "text", "content": "yo"}]}
        ],
    }
    msgs = llm_messages(attrs, [])
    assert [(m.role, m.direction, m.parts[0].content) for m in msgs] == [
        ("user", "input", "hi"),
        ("assistant", "output", "yo"),
    ]
