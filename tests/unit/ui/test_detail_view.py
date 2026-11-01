"""Span drawer view model (plan task P6-06.2; REQ-151, 152, 327)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from reflex_agentlens.query.dto import (
    ApprovalView,
    ErrorInfo,
    LLMCallView,
    Message,
    MessagePart,
    SpanDetail,
    SpanEventRaw,
    SpanRaw,
    ToolCallView,
)
from reflex_agentlens.views.detail import MAX_JSON_CHARS, drawer_view, pretty_json, raw_sections
from tests.unit.ui.factories import MS, T0, span

RAW = SpanRaw(
    span_id="a" * 16,
    attributes={"k": "v"},
    resource={"service.name": "api"},
    scope_name="openai",
    scope_version="1.2",
    events=[],
    links=[],
    dropped_keys=[],
)


def llm(**kw: Any) -> LLMCallView:
    base: dict[str, Any] = {
        "span_id": "a" * 16,
        "operation": "chat",
        "provider": "openai",
        "request_model": "gpt-4o",
        "response_model": "gpt-4o-2024",
        "model": "gpt-4o-2024",
        "params": {"temperature": 0.2, "max_tokens": 100},
        "input_tokens": 1200,
        "output_tokens": 30,
        "cached_input_tokens": 200,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": Decimal("0.004"),
        "unpriced": False,
        "pricing_version": 3,
        "status": "ok",
        "error_type": None,
        "start_ns": T0,
        "duration_ns": 900 * MS,
        "finish_reasons": ["stop"],
        "messages": [],
        "content_dropped": False,
    }
    base.update(kw)
    return LLMCallView(**base)


def detail(**kw: Any) -> SpanDetail:
    base: dict[str, Any] = {
        "span": span("a", None, 0, 0, 900, kind="llm", is_genai=True),
        "llm": None,
        "tool": None,
        "error": None,
        "raw": RAW,
    }
    base.update(kw)
    return SpanDetail(**base)


def test_llm_call_sections_req_151() -> None:
    messages = [
        Message(
            role="system", direction="input", parts=[MessagePart(type="text", content="Be brief")]
        ),
        Message(
            role="user",
            direction="input",
            name="ana",
            parts=[MessagePart(type="text", content="**refund** <script>x</script>")],
        ),
        Message(
            role="assistant",
            direction="output",
            finish_reason="tool_calls",
            parts=[
                MessagePart(type="reasoning", content="think"),
                MessagePart(
                    type="tool_call", id="c1", name="search", arguments_json='{"q":"a","n":1}'
                ),
            ],
        ),
        Message(
            role="tool",
            direction="input",
            parts=[
                MessagePart(type="tool_result", id="c1", result_json="not json"),
                MessagePart(type="other", raw_type="image", raw_json='{"url":"x"}'),
            ],
        ),
    ]
    view = drawer_view(detail(llm=llm(messages=messages)))
    llm_rows = {p.label: p.value for p in view.llm}
    assert llm_rows["Model"] == "gpt-4o-2024"
    assert llm_rows["Requested model"] == "gpt-4o"
    assert llm_rows["Parameters"] == "max_tokens=100, temperature=0.2"
    usage = {p.label: p.value for p in view.usage}
    assert usage == {
        "Input tokens": "1,200",
        "Cached input tokens": "200",
        "Output tokens": "30",
        "Cost": "$0.0040",
        "Pricing version": "3",
    }
    assert [b.title for b in view.messages] == [
        "System",
        "User · ana",
        "Assistant · tool_calls",
        "Tool",
    ]
    user_text = view.messages[1].parts[0]
    assert user_text.markdown  # rendered by the safe rich-text component
    assert user_text.body == "**refund** <script>x</script>"  # unchanged: never interpreted here
    call = view.messages[2].parts[1]
    assert (call.title, call.markdown) == ("Tool call: search (c1)", False)
    assert call.body == '{\n  "n": 1,\n  "q": "a"\n}'
    result, other = view.messages[3].parts
    assert result.body == "not json"
    assert other.title == "image"


def test_tool_error_and_badges_req_151_152() -> None:
    tool = ToolCallView(
        span_id="a" * 16,
        tool_name="search",
        tool_call_id="c1",
        tool_type=None,
        status="error",
        error_type="Timeout",
        start_ns=T0,
        duration_ns=MS,
        arguments_json='{"q": 1}',
        result_json=None,
        content_dropped=True,
    )
    view = drawer_view(
        detail(
            span=span(
                "a",
                None,
                0,
                0,
                1,
                kind="tool",
                status="error",
                status_message="boom",
                redacted=True,
                missing_parent=True,
            ),
            tool=tool,
            error=ErrorInfo(message="boom", exception_type="TimeoutError", stacktrace="Traceback…"),
        )
    )
    assert {p.label: p.value for p in view.tool}["Error type"] == "Timeout"
    assert view.tool_arguments == '{\n  "q": 1\n}'
    assert view.tool_result == ""
    assert view.status == "error"
    assert {p.label: p.value for p in view.error} == {
        "Exception": "TimeoutError",
        "Message": "boom",
    }
    assert view.stacktrace == "Traceback…"
    assert view.badges == ["redacted", "missing parent"]
    assert view.content_dropped
    assert view.messages == []


def test_approval_and_unpriced() -> None:
    approval = ApprovalView(
        kind="approval",
        decision="approved",
        approval_key=None,
        approver="ops",
        requested_ns=T0,
        decided_ns=T0 + 2000 * MS,
        wait_ns=2000 * MS,
        tool_call_id=None,
        tool_name="refund",
        node_name=None,
    )
    view = drawer_view(
        detail(
            span=span("a", None, 0, 0, 1, kind="approval", approval=approval),
            llm=llm(cost_usd=None, unpriced=True, pricing_version=None),
        )
    )
    rows = {p.label: p.value for p in view.approval}
    assert rows["Decision"] == "approved"
    assert rows["Waited"] == "2.00 s"
    assert {p.label: p.value for p in view.usage}["Cost"] == "unpriced"


def test_json_is_capped() -> None:
    big = '"' + "x" * (MAX_JSON_CHARS + 10) + '"'
    assert pretty_json(big).endswith("more characters")
    assert pretty_json(None) == ""


def test_raw_sections_req_151e() -> None:
    raw = RAW.model_copy(
        update={
            "events": [SpanEventRaw(name="exception", time_ns=T0, attributes={"a": 1})],
            "dropped_keys": ["gen_ai.input.messages"],
        }
    )
    titles = [s.title for s in raw_sections(raw)]
    assert titles == ["Attributes", "Resource", "Instrumentation scope", "Events", "Dropped keys"]
    assert raw_sections(raw)[2].body == "openai 1.2"
