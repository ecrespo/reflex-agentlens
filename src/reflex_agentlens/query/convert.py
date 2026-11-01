"""``store.records`` → query DTOs (plan task P4-01.1; API §6.7, Data Model §8).

The only place where storage types become DTOs: hex IDs (``core.ids``), ``Decimal`` money
(``core.money``), Source labels, and "priced sum + flag" totals (REQ-061.c).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any, Literal

from reflex_agentlens.core.ids import span_id_to_hex, trace_id_to_hex
from reflex_agentlens.core.money import nusd_per_mtok_to_price, nusd_to_usd
from reflex_agentlens.core.semconv import messages as sm
from reflex_agentlens.query import dto
from reflex_agentlens.store import records as r

__all__ = [
    "FLAG_CONTENT_DROPPED",
    "FLAG_REDACTED",
    "FLAG_TRUNCATED",
    "api_key_view",
    "approval_stats",
    "approval_view",
    "conversation_messages",
    "json_text",
    "kpis",
    "latency_rows",
    "llm_call_view",
    "model_usage",
    "money",
    "optional_money",
    "pricing_table",
    "pricing_table_entry",
    "pricing_version_info",
    "project",
    "project_ref",
    "purge_view",
    "redaction_record",
    "redaction_rules",
    "run_summary",
    "saved_filter_view",
    "session_detail",
    "session_summary",
    "source_ref",
    "source_usage",
    "source_view",
    "span_raw",
    "time_series",
    "tool_call_view",
    "tool_stats",
    "unpriced_model",
]

# spans.flags bits (Data Model §2.10)
FLAG_TRUNCATED = 1
FLAG_REDACTED = 2
FLAG_CONTENT_DROPPED = 4

DAY_S = 86_400
_ROLES = {"system", "user", "assistant", "tool"}


def money(nusd: int) -> Decimal:
    return nusd_to_usd(nusd)


def optional_money(nusd: int | None) -> Decimal | None:
    return None if nusd is None else nusd_to_usd(nusd)


def json_text(value: Any) -> str | None:
    """A JSON value as display text: strings stay as they are, anything else is JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def source_ref(source: r.SourceKey | None) -> dto.SourceRef | None:
    if source is None:
        return None
    return dto.SourceRef(
        service_name=source.service_name, service_namespace=source.service_namespace
    )


def _source(source: r.SourceKey) -> dto.SourceRef:
    return dto.SourceRef(
        service_name=source.service_name, service_namespace=source.service_namespace
    )


# ── runs and spans ───────────────────────────────────────────────────────────────────────────


def run_summary(run: r.RunSummary) -> dto.RunSummary:
    return dto.RunSummary(
        project_id=run.project_id,
        trace_id=trace_id_to_hex(run.trace_id),
        start_ns=run.start_ns,
        end_ns=run.end_ns,
        duration_ns=run.duration_ns,
        last_span_ns=run.last_span_ns,
        status=run.status,
        root_name=run.root_name,
        agent_name=run.agent_name,
        source=source_ref(run.source),
        source_count=run.source_count,
        models=list(run.models),
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cached_input_tokens=run.cached_input_tokens,
        total_tokens=run.input_tokens + run.output_tokens,
        usage_source=run.usage_source,
        cost_usd=optional_money(run.cost_nusd),
        unpriced=run.unpriced_calls > 0,
        llm_calls=run.llm_calls,
        tool_calls=run.tool_calls,
        error_spans=run.error_spans,
        span_count=run.span_count,
        missing_parents=run.missing_parents,
        session_id=run.session_id,
        is_genai=run.is_genai,
    )


def _param(value: Any) -> float | int | str | bool | None:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _part(part: sm.Part) -> dto.MessagePart:
    return dto.MessagePart(
        type=part.type,
        content=part.content,
        id=part.id,
        name=part.name,
        arguments_json=json_text(part.arguments),
        result_json=json_text(part.result),
        raw_type=part.raw_type,
        raw_json=part.raw_json,
    )


def _message(message: sm.Message, direction: Literal["input", "output"]) -> dto.Message:
    role = message.role if message.role in _ROLES else "other"
    return dto.Message(
        role=role,  # type: ignore[arg-type]
        name=message.name,
        finish_reason=message.finish_reason,
        direction=direction,
        parts=[_part(p) for p in message.parts],
    )


def conversation_messages(conversation: sm.Conversation) -> list[dto.Message]:
    """Input messages, then output messages (REQ-034); unknown roles become ``other``."""
    return [_message(m, "input") for m in conversation.input] + [
        _message(m, "output") for m in conversation.output
    ]


def llm_call_view(
    span_id: bytes, call: r.LLMCallRow, messages: Sequence[dto.Message] = ()
) -> dto.LLMCallView:
    return dto.LLMCallView(
        span_id=span_id_to_hex(span_id),
        operation=call.operation,
        provider=call.provider,
        request_model=call.request_model,
        response_model=call.response_model,
        model=call.model,
        params={k: _param(v) for k, v in call.params.items()},
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cached_input_tokens=call.cached_input_tokens,
        cache_write_tokens=call.cache_write_tokens,
        reasoning_tokens=call.reasoning_tokens,
        cost_usd=optional_money(call.cost_nusd),
        unpriced=call.unpriced,
        pricing_version=call.pricing_version,
        status=call.status,
        error_type=call.error_type,
        start_ns=call.start_ns,
        duration_ns=call.duration_ns,
        finish_reasons=list(call.finish_reasons),
        messages=list(messages),
        content_dropped=call.content_dropped,
    )


def tool_call_view(
    span_id: bytes,
    call: r.ToolCallRow,
    arguments: Any = None,
    result: Any = None,
) -> dto.ToolCallView:
    return dto.ToolCallView(
        span_id=span_id_to_hex(span_id),
        tool_name=call.tool_name,
        tool_call_id=call.tool_call_id,
        tool_type=call.tool_type,
        status=call.status,
        error_type=call.error_type,
        start_ns=call.start_ns,
        duration_ns=call.duration_ns,
        arguments_json=json_text(arguments),
        result_json=json_text(result),
        content_dropped=call.content_dropped,
    )


def approval_view(row: r.ApprovalRow) -> dto.ApprovalView:
    return dto.ApprovalView(
        kind=row.kind,
        decision=row.decision,
        approval_key=row.approval_key,
        approver=row.approver,
        requested_ns=row.requested_ns,
        decided_ns=row.decided_ns,
        wait_ns=row.wait_ns,
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        node_name=row.node_name,
    )


def _event(raw: Any) -> dto.SpanEventRaw:
    ev = raw if isinstance(raw, dict) else {}
    attrs = ev.get("attributes")
    return dto.SpanEventRaw(
        name=str(ev.get("name", "")),
        time_ns=int(ev.get("time_ns") or 0),
        attributes=attrs if isinstance(attrs, dict) else {},
    )


def _link(raw: Any) -> dto.SpanLinkRaw:
    link = raw if isinstance(raw, dict) else {}
    attrs = link.get("attributes")
    return dto.SpanLinkRaw(
        trace_id=str(link.get("trace_id_hex", "")),
        span_id=str(link.get("span_id_hex", "")),
        attributes=attrs if isinstance(attrs, dict) else {},
    )


def span_raw(raw: r.SpanRaw) -> dto.SpanRaw:
    return dto.SpanRaw(
        span_id=span_id_to_hex(raw.span_id),
        attributes=dict(raw.attributes),
        resource=dict(raw.resource),
        scope_name=raw.scope_name,
        scope_version=raw.scope_version,
        events=[_event(e) for e in raw.events],
        links=[_link(link) for link in raw.links],
        dropped_keys=list(raw.dropped_keys),
    )


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


def session_summary(s: r.SessionSummary) -> dto.SessionSummary:
    return dto.SessionSummary(
        project_id=s.project_id,
        session_id=s.session_id,
        external_id=s.external_id,
        first_ns=s.first_ns,
        last_ns=s.last_ns,
        run_count=s.run_count,
        input_tokens=s.input_tokens,
        output_tokens=s.output_tokens,
        cached_input_tokens=s.cached_input_tokens,
        total_tokens=s.input_tokens + s.output_tokens,
        cost_usd=money(s.cost_nusd),
        includes_unpriced=s.includes_unpriced,
    )


def session_detail(d: r.SessionDetail) -> dto.SessionDetail:
    return dto.SessionDetail(
        session=session_summary(d.session),
        runs=[
            dto.SessionRunView(
                run=run_summary(sr.run),
                cumulative_tokens=sr.cumulative_tokens,
                cumulative_cost_usd=money(sr.cumulative_cost_nusd),
                cumulative_includes_unpriced=sr.cumulative_includes_unpriced,
            )
            for sr in d.runs
        ],
    )


# ── dashboard ────────────────────────────────────────────────────────────────────────────────


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def kpis(k: r.Kpis) -> dto.Kpis:
    return dto.Kpis(
        runs=k.runs,
        error_runs=k.error_runs,
        error_rate=_rate(k.error_runs, k.runs),
        input_tokens=k.input_tokens,
        output_tokens=k.output_tokens,
        total_tokens=k.total_tokens,
        cost_usd=money(k.cost_nusd),
        unpriced_runs=k.unpriced_runs,
        includes_unpriced=k.includes_unpriced,
        p95_duration_ns=k.p95_duration_ns,
        active_sessions=k.active_sessions,
    )


def time_series(ts: r.TimeSeries) -> dto.TimeSeries:
    """Store groups are tuples; the DTO joins them with ``/`` (one dimension in practice)."""
    points = [
        dto.SeriesPoint(bucket_ns=p.bucket_ns, group="/".join(p.group), value=p.value)
        for p in ts.points
    ]
    groups = sorted({p.group for p in points})
    return dto.TimeSeries(bucket_ns=ts.bucket_ns, groups=groups, points=points)


def _model_row(row: r.ModelUsageRow) -> dto.ModelUsageRow:
    return dto.ModelUsageRow(
        model=row.model,
        bucket_ns=row.bucket_ns,
        calls=row.calls,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cached_input_tokens=row.cached_input_tokens,
        cost_usd=money(row.cost_nusd),
        unpriced_calls=row.unpriced_calls,
        includes_unpriced=row.includes_unpriced,
    )


def model_usage(mu: r.ModelUsage) -> dto.ModelUsage:
    return dto.ModelUsage(
        bucket_ns=mu.bucket_ns,
        totals=[_model_row(x) for x in mu.totals],
        over_time=[_model_row(x) for x in mu.over_time],
    )


def latency_rows(rows: Iterable[r.LatencyRow]) -> list[dto.LatencyRow]:
    return [
        dto.LatencyRow(
            group=x.group, count=x.count, p50_ns=x.p50_ns, p95_ns=x.p95_ns, p99_ns=x.p99_ns
        )
        for x in rows
    ]


def tool_stats(rows: Iterable[r.ToolStatRow]) -> list[dto.ToolStat]:
    return [
        dto.ToolStat(
            tool_name=x.tool_name,
            calls=x.calls,
            successes=x.successes,
            errors=x.errors,
            error_rate=_rate(x.errors, x.calls),
        )
        for x in rows
    ]


def source_usage(rows: Iterable[r.SourceUsageRow]) -> list[dto.SourceUsage]:
    return [
        dto.SourceUsage(
            source=_source(x.source),
            runs=x.runs,
            input_tokens=x.input_tokens,
            output_tokens=x.output_tokens,
            cost_usd=money(x.cost_nusd),
            unpriced_runs=x.unpriced_runs,
            includes_unpriced=x.unpriced_runs > 0,
        )
        for x in rows
    ]


def approval_stats(stats: r.ApprovalStats) -> dto.ApprovalStats:
    return dto.ApprovalStats(
        by_decision=[
            dto.ApprovalDecisionCount(
                decision=x.decision, count=x.count, median_wait_ns=x.median_wait_ns
            )
            for x in stats.by_decision
        ],
        median_wait_ns=stats.median_wait_ns,
    )


# ── selectors and storage ────────────────────────────────────────────────────────────────────


def project_ref(p: r.Project) -> dto.ProjectRef:
    return dto.ProjectRef(id=p.id, slug=p.slug, name=p.name)


def source_view(v: r.SourceView) -> dto.SourceView:
    return dto.SourceView(
        project_id=v.project_id,
        source=_source(v.source),
        first_seen_ns=v.first_seen_ns,
        last_seen_ns=v.last_seen_ns,
    )


def purge_view(p: r.PurgeReport | None) -> dto.PurgeView | None:
    if p is None:
        return None
    return dto.PurgeView(
        trigger=p.trigger,
        started_ns=p.started_ns,
        finished_ns=p.finished_ns,
        deleted_traces=p.deleted_traces_age + p.deleted_traces_size,
        deleted_bytes_est=p.deleted_bytes_est,
        error_class=p.error_class,
    )


# ── admin ────────────────────────────────────────────────────────────────────────────────────


def project(p: r.Project) -> dto.Project:
    return dto.Project(
        id=p.id,
        slug=p.slug,
        name=p.name,
        content_storage=p.content_storage,
        retention_days=p.retention_max_age_s // DAY_S,
        request_rate=p.request_rate,
        request_burst=p.request_burst,
        span_rate=p.span_rate,
        span_burst=p.span_burst,
        settings_version=p.settings_version,
        created_at_ns=p.created_at_ns,
        updated_at_ns=p.updated_at_ns,
        updated_by=p.updated_by,
    )


def api_key_view(k: r.ApiKeyRecord) -> dto.ApiKeyView:
    return dto.ApiKeyView(
        key_id=k.key_id,
        prefix=f"al_{k.key_id}_",
        label=k.label,
        created_at_ns=k.created_at_ns,
        created_by=k.created_by,
        last_used_at_ns=k.last_used_at_ns,
        revoked_at_ns=k.revoked_at_ns,
        revoked=k.revoked_at_ns is not None,
    )


def _price(nusd_per_mtok: int | None) -> Decimal | None:
    return None if nusd_per_mtok is None else nusd_per_mtok_to_price(nusd_per_mtok)


def pricing_version_info(m: r.PricingVersionMeta) -> dto.PricingVersionInfo:
    return dto.PricingVersionInfo(
        version=m.version,
        effective_from_ns=m.effective_from_ns,
        source=m.source,
        note=m.note,
        created_at_ns=m.created_at_ns,
        created_by=m.created_by,
    )


def pricing_table(v: r.PricingVersion) -> dto.PricingTable:
    return dto.PricingTable(
        info=pricing_version_info(v.meta),
        entries=[
            dto.PricingEntryView(
                provider=e.provider,
                model_pattern=e.model_pattern,
                match_kind=e.match_kind,
                input_usd_per_mtok=nusd_per_mtok_to_price(e.input_nusd_per_mtok),
                output_usd_per_mtok=nusd_per_mtok_to_price(e.output_nusd_per_mtok),
                cached_input_usd_per_mtok=_price(e.cached_input_nusd_per_mtok),
                cache_write_usd_per_mtok=_price(e.cache_write_nusd_per_mtok),
            )
            for e in v.entries
        ],
    )


def unpriced_model(m: r.UnpricedModel) -> dto.UnpricedModel:
    return dto.UnpricedModel(
        provider=m.provider, model=m.model, calls=m.calls, last_seen_ns=m.last_seen_ns
    )


def saved_filter_view(f: r.SavedFilter) -> dto.SavedFilterView:
    return dto.SavedFilterView(
        id=f.id,
        project_id=f.project_id,
        name=f.name,
        query=f.query,
        created_by=f.created_by,
        created_at_ns=f.created_at_ns,
        updated_at_ns=f.updated_at_ns,
    )


def pricing_table_entry(e: r.PricingEntry) -> dto.PricingEntryView:
    return dto.PricingEntryView(
        provider=e.provider,
        model_pattern=e.model_pattern,
        match_kind=e.match_kind,
        input_usd_per_mtok=nusd_per_mtok_to_price(e.input_nusd_per_mtok),
        output_usd_per_mtok=nusd_per_mtok_to_price(e.output_nusd_per_mtok),
        cached_input_usd_per_mtok=_price(e.cached_input_nusd_per_mtok),
        cache_write_usd_per_mtok=_price(e.cache_write_nusd_per_mtok),
    )


_PRESETS = ("secrets", "email", "phone", "credit_card")


def redaction_rules(rules: r.RedactionRuleSet) -> dto.RedactionRuleSet:
    """Stored rule set → DTO (the store keeps rules as validated JSON objects)."""
    presets = {name: rules.presets.get(name) is True for name in _PRESETS}
    patterns = [
        dto.PatternRule(
            name=str(x.get("name") or f"rule{i + 1}"),
            regex=str(x["regex"]),
            enabled=x.get("enabled", True) is not False,
        )
        for i, x in enumerate(rules.pattern_rules)
        if isinstance(x, dict) and isinstance(x.get("regex"), str)
    ]
    fields = [
        dto.FieldRule(
            key_glob=str(x["key_glob"]), action="drop" if x.get("action") == "drop" else "mask"
        )
        for x in rules.field_rules
        if isinstance(x, dict) and isinstance(x.get("key_glob"), str)
    ]
    return dto.RedactionRuleSet(presets=presets, pattern_rules=patterns, field_rules=fields)  # type: ignore[arg-type]


def redaction_record(
    rules: dto.RedactionRuleSet, author: str | None, at_ns: int
) -> r.RedactionRuleSet:
    """DTO → the stored JSON form read by ``ingest.redaction``."""
    return r.RedactionRuleSet(
        presets={name: bool(rules.presets.get(name, False)) for name in _PRESETS},  # type: ignore[call-overload]
        pattern_rules=[
            {"name": p.name, "regex": p.regex, "enabled": p.enabled} for p in rules.pattern_rules
        ],
        field_rules=[{"key_glob": f.key_glob, "action": f.action} for f in rules.field_rules],
        updated_at_ns=at_ns,
        updated_by=author,
    )
