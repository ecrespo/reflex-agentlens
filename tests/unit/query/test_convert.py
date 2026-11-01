"""Records → DTO conversions (plan task P4-01.1; API §6.7, Data Model §8, Art. 5)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from reflex_agentlens.core.semconv import messages as sm
from reflex_agentlens.query import convert, dto
from reflex_agentlens.store import records as r

TRACE = bytes(range(1, 17))
SPAN = bytes(range(1, 9))


def _run(**kw: object) -> r.RunSummary:
    base: dict[str, object] = {
        "project_id": "p1", "trace_id": TRACE, "start_ns": 10, "end_ns": 30, "duration_ns": 20,
        "last_span_ns": 30, "status": "ok", "root_name": "root", "agent_name": "triage",
        "source": r.SourceKey("svc", "ns"), "source_count": 1, "models": ["gpt-4o"],
        "input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 10,
        "usage_source": "llm", "cost_nusd": 1_234_567_891,
        "unpriced_calls": 0, "llm_calls": 2, "tool_calls": 1, "error_spans": 0, "span_count": 5,
        "missing_parents": 0, "session_id": None, "is_genai": True,
    }  # fmt: skip
    base.update(kw)
    return r.RunSummary(**base)  # type: ignore[arg-type]


def test_run_summary_req_100b() -> None:
    """REQ-100.b: hex trace ID, exact Decimal cost, Source label, total tokens."""
    out = convert.run_summary(_run())
    assert out.trace_id == TRACE.hex()
    assert out.cost_usd == Decimal("1.234567891")
    assert out.unpriced is False
    assert out.total_tokens == 150
    assert out.source is not None
    assert out.source.label == "ns/svc"
    assert out.ref == dto.RunRef(project_id="p1", trace_id=TRACE.hex())


def test_run_summary_unpriced_and_agent_usage() -> None:
    """REQ-061.c / DD-012: unpriced → flag; no priced call → cost None; agent usage kept."""
    out = convert.run_summary(_run(cost_nusd=None, unpriced_calls=2, usage_source="agent"))
    assert out.cost_usd is None
    assert out.unpriced is True
    assert out.usage_source == "agent"


def test_run_summary_multi_source_and_no_source() -> None:
    out = convert.run_summary(_run(source=None, source_count=3))
    assert out.source is None
    assert out.source_count == 3


def test_source_label_without_namespace() -> None:
    assert dto.SourceRef(service_name="svc").label == "svc"
    assert dto.SourceRef(service_name="svc", service_namespace="ns").label == "ns/svc"


def test_dto_json_money_is_string_and_unknown_fields_rejected() -> None:
    """API §2: money serialized as a string; DTOs are frozen and forbid extra fields."""
    out = convert.run_summary(_run())
    data = json.loads(out.model_dump_json())
    assert data["cost_usd"] == "1.234567891"
    assert data["source"]["label"] == "ns/svc"
    with pytest.raises(PydanticValidationError):
        dto.RunRef(project_id="p", trace_id="t", extra="x")  # type: ignore[call-arg]
    with pytest.raises(PydanticValidationError):
        out.status = "error"


def test_llm_call_view_and_messages_req_104() -> None:
    call = r.LLMCallRow(
        start_ns=1, duration_ns=2, operation="chat", unpriced=False, model="gpt-4o",
        input_tokens=3, output_tokens=4, cost_nusd=500, pricing_version=1,
        params={"temperature": 0.2, "stop": ["x"]}, finish_reasons=["stop"],
    )  # fmt: skip
    conv = sm.Conversation(
        input=(
            sm.Message("developer", (sm.Part("text", content="be brief"),)),
            sm.Message(
                "user",
                (sm.Part("tool_result", id="c1", result={"ok": True}),),
            ),
        ),
        output=(
            sm.Message(
                "assistant",
                (sm.Part("tool_call", id="c2", name="search", arguments={"q": "x"}),),
                finish_reason="tool_calls",
            ),
        ),
    )
    msgs = convert.conversation_messages(conv)
    view = convert.llm_call_view(SPAN, call, msgs)
    assert view.span_id == SPAN.hex()
    assert view.cost_usd == Decimal("0.0000005")
    assert view.params == {"temperature": 0.2, "stop": '["x"]'}
    assert [m.role for m in view.messages] == ["other", "user", "assistant"]
    assert [m.direction for m in view.messages] == ["input", "input", "output"]
    assert view.messages[1].parts[0].result_json == '{"ok": true}'
    assert view.messages[2].parts[0].arguments_json == '{"q": "x"}'
    assert view.messages[2].finish_reason == "tool_calls"


def test_tool_and_approval_views() -> None:
    tool = r.ToolCallRow(start_ns=1, duration_ns=2, status="error", tool_name="t", error_type="E")
    view = convert.tool_call_view(SPAN, tool, arguments="plain text", result={"a": 1})
    assert view.arguments_json == "plain text"
    assert view.result_json == '{"a": 1}'
    appr = r.ApprovalRow(kind="approval", decision="approved", requested_ns=1, wait_ns=5)
    assert convert.approval_view(appr).wait_ns == 5


def test_span_raw_events_and_links() -> None:
    raw = r.SpanRaw(
        span_id=SPAN, attributes={"a": 1}, resource={"service.name": "svc"},
        scope_name="s", scope_version=None,
        events=[{"name": "exception", "time_ns": 5, "attributes": {"exception.type": "E"}}],
        links=[{"trace_id_hex": "ab", "span_id_hex": "cd", "attributes": {}}],
        dropped_keys=["gen_ai.input.messages"],
    )  # fmt: skip
    out = convert.span_raw(raw)
    assert out.events[0].name == "exception"
    assert out.events[0].time_ns == 5
    assert out.links[0].trace_id == "ab"
    assert out.dropped_keys == ["gen_ai.input.messages"]


def test_session_conversions_req_105() -> None:
    s = r.SessionSummary(
        project_id="p", session_id="s", external_id="conv-1", first_ns=1, last_ns=2,
        run_count=2, input_tokens=5, output_tokens=6, cached_input_tokens=0, cost_nusd=10,
        includes_unpriced=True,
    )  # fmt: skip
    detail = convert.session_detail(
        r.SessionDetail(s, [r.SessionRun(_run(), 150, 1_234_567_891, False)])
    )
    assert detail.session.total_tokens == 11
    assert detail.session.includes_unpriced
    assert detail.runs[0].cumulative_cost_usd == Decimal("1.234567891")


def test_dashboard_conversions_req_106() -> None:
    k = convert.kpis(r.Kpis(4, 1, 10, 20, 3_000_000_000, 1, 99, 2))
    assert k.error_rate == 0.25
    assert k.total_tokens == 30
    assert k.cost_usd == Decimal(3)
    assert k.includes_unpriced
    assert convert.kpis(r.Kpis(0, 0, 0, 0, 0, 0, None, 0)).error_rate == 0.0
    ts = convert.time_series(
        r.TimeSeries(60, [r.SeriesPoint(0, ("ok",), 2), r.SeriesPoint(0, ("error",), 1)])
    )
    assert ts.groups == ["error", "ok"]
    assert ts.points[0].group == "ok"
    tools = convert.tool_stats([r.ToolStatRow("t", 4, 1)])
    assert tools[0].successes == 3
    assert tools[0].error_rate == 0.25
    src = convert.source_usage([r.SourceUsageRow(r.SourceKey("svc"), 1, 2, 3, 4, 1)])
    assert src[0].includes_unpriced
    assert src[0].source.label == "svc"
    mu = convert.model_usage(r.ModelUsage(60, [r.ModelUsageRow("m", None, 1, 2, 3, 0, 5, 1)], []))
    assert mu.totals[0].includes_unpriced
    lat = convert.latency_rows([r.LatencyRow("chat", 3, 1, 2, 3)])
    assert lat[0].p95_ns == 2
    appr = convert.approval_stats(r.ApprovalStats([r.ApprovalDecisionRow("approved", 2, 7)], 7))
    assert appr.by_decision[0].count == 2


def test_admin_conversions() -> None:
    p = r.Project(
        id="p", slug="s", name="n", content_storage=True, retention_max_age_s=30 * 86_400,
        request_rate=1, request_burst=2, span_rate=3, span_burst=4, settings_version=1,
        created_at_ns=0, updated_at_ns=0, updated_by=None,
    )  # fmt: skip
    assert convert.project(p).retention_days == 30
    assert convert.project_ref(p).slug == "s"
    key = r.ApiKeyRecord(
        key_id="abcd1234", project_id="p", label="ci", salt=b"s", secret_hash=b"h",
        created_at_ns=1, created_by=None, last_used_at_ns=None, revoked_at_ns=9,
    )  # fmt: skip
    view = convert.api_key_view(key)
    assert view.prefix == "al_abcd1234_"
    assert view.revoked
    assert "secret_hash" not in view.model_dump()
    meta = r.PricingVersionMeta(2, 0, "editor", None, 1, "admin")
    table = convert.pricing_table(
        r.PricingVersion(meta, (r.PricingEntry("gpt-*", "glob", 2_500_000_000, 0, "openai"),))
    )
    assert table.entries[0].input_usd_per_mtok == Decimal("2.5")
    assert table.entries[0].cached_input_usd_per_mtok is None
    report = r.PurgeReport("manual", 1, 2, 3, 4, 5)
    purge = convert.purge_view(report)
    assert purge is not None
    assert purge.deleted_traces == 7
    assert convert.purge_view(None) is None


def test_input_models_validate() -> None:
    """API §6.7 inputs: ranges need an end and min ≤ max; predicates check eq/exists."""
    assert dto.Range[int](min=1).max is None
    with pytest.raises(PydanticValidationError):
        dto.Range[int]()
    with pytest.raises(PydanticValidationError):
        dto.Range[int](min=3, max=1)
    with pytest.raises(PydanticValidationError):
        dto.AttrPredicate(key="a")
    with pytest.raises(PydanticValidationError):
        dto.AttrPredicate(key="a", op="exists", value="x")
    with pytest.raises(PydanticValidationError):
        dto.AttrPredicate(key="bad key", value="x")
    with pytest.raises(PydanticValidationError):
        dto.ProjectSettingsPatch(retention_days=-1)
    with pytest.raises(PydanticValidationError):
        dto.PatternRule(name="Bad-Name", regex="x")


def test_created_api_key_masks_plaintext() -> None:
    created = dto.CreatedApiKey(key_id="k", plaintext="al_k_secret", snippet="x")  # type: ignore[arg-type]
    assert "al_k_secret" not in repr(created)
    assert created.plaintext.get_secret_value() == "al_k_secret"
