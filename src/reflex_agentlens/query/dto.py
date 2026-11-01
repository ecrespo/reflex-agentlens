"""Query DTOs (plan task P4-01.1; API §6.7, Analyze A-05).

Frozen Pydantic v2 models, JSON-serializable and Reflex-state friendly. They are built from
``store.records`` in :mod:`reflex_agentlens.query.convert` only. Conventions (API §2, §6.7):
hex IDs, ``int`` nanoseconds, ``Decimal`` USD (a string in JSON), "priced sum + flag" totals.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, computed_field, model_validator

__all__ = [
    "AgentGraph",
    "AggScope",
    "ApiKeyView",
    "ApprovalDecisionCount",
    "ApprovalStats",
    "ApprovalView",
    "AttrPredicate",
    "ChangeKind",
    "ChangeNotice",
    "CreatedApiKey",
    "DashboardData",
    "ErrorInfo",
    "FacetField",
    "FieldRule",
    "GraphEdge",
    "GraphNode",
    "Kpis",
    "LLMCallView",
    "LatencyRow",
    "Message",
    "MessagePart",
    "ModelUsage",
    "ModelUsageRow",
    "Page",
    "PatternRule",
    "PricingEntryView",
    "PricingTable",
    "PricingVersionInfo",
    "Project",
    "ProjectRef",
    "ProjectSettingsPatch",
    "PurgeView",
    "Range",
    "RedactionRuleSet",
    "RunRef",
    "RunSort",
    "RunStatus",
    "RunSummary",
    "SavedFilterView",
    "SeriesPoint",
    "SessionDetail",
    "SessionFilter",
    "SessionRunView",
    "SessionSort",
    "SessionSummary",
    "SourceRef",
    "SourceUsage",
    "SourceView",
    "SpanDetail",
    "SpanEventRaw",
    "SpanKind",
    "SpanLinkRaw",
    "SpanNode",
    "SpanRaw",
    "StorageStatus",
    "TimeExpr",
    "TimeSeries",
    "TokenCostTotals",
    "ToolCallView",
    "ToolStat",
    "TraceDetail",
    "UnpricedModel",
    "UsageSource",
    "Viewer",
]

T = TypeVar("T")
N = TypeVar("N", int, Decimal)

RunStatus = Literal["in_progress", "ok", "error", "incomplete"]
RunSort = Literal[
    "start_desc", "start_asc", "duration_desc", "duration_asc",
    "tokens_desc", "tokens_asc", "cost_desc", "cost_asc",
]  # fmt: skip
SessionSort = Literal[
    "last_desc", "last_asc", "runs_desc", "runs_asc",
    "tokens_desc", "tokens_asc", "cost_desc", "cost_asc",
]  # fmt: skip
FacetField = Literal["model", "tool", "status", "source", "agent"]
SpanKind = Literal["llm", "tool", "agent", "approval", "generic"]
UsageSource = Literal["llm", "agent", "none"]
ChangeKind = Literal["spans_committed", "runs_finalized", "purged", "settings_changed"]
OkError = Literal["ok", "error"]
TimeExpr = str  # RFC 3339 with offset | "now" | "now-<n><s|m|h|d|w>" (query.timeexpr)
JsonValue = Any  # a JSON value as stored (Data Model §1.4)


class _Dto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who is asking (API §6). ``allowed_project_ids=None`` means every Project [REQ-191.c]."""

    id: str
    display_name: str
    is_admin: bool
    allowed_project_ids: frozenset[str] | None = None


# ── inputs ───────────────────────────────────────────────────────────────────────────────────


class RunRef(_Dto):
    project_id: str
    trace_id: str


class SourceRef(_Dto):
    service_name: str
    service_namespace: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def label(self) -> str:
        """``ns/name``, or ``name`` when the namespace is empty (URLs, filters, pickers)."""
        if self.service_namespace:
            return f"{self.service_namespace}/{self.service_name}"
        return self.service_name


class Range(_Dto, Generic[N]):
    """Inclusive range with at least one end."""

    min: N | None = None
    max: N | None = None

    @model_validator(mode="after")
    def _check(self) -> Range[N]:
        if self.min is None and self.max is None:
            raise ValueError("a range needs at least one end")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is greater than max")
        return self


class AttrPredicate(_Dto):
    scope: Literal["root", "resource"] = "root"
    key: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,256}$")
    op: Literal["eq", "exists"] = "eq"
    value: str | None = None

    @model_validator(mode="after")
    def _check(self) -> AttrPredicate:
        if self.op == "eq" and self.value is None:
            raise ValueError("eq needs a value")
        if self.op == "exists" and self.value is not None:
            raise ValueError("exists takes no value")
        return self


class SessionFilter(_Dto):
    project_ids: list[str] = []
    sources: list[str] = []
    time_from: TimeExpr | None = None
    time_to: TimeExpr | None = None


class AggScope(_Dto):
    project_ids: list[str] = []
    sources: list[str] = []
    time_from: TimeExpr = "now-24h"
    time_to: TimeExpr = "now"
    bucket: Literal["auto", "minute", "hour", "day"] = "auto"


class Page(_Dto, Generic[T]):
    items: list[T]
    next_cursor: str | None


# ── runs and traces ──────────────────────────────────────────────────────────────────────────


class RunSummary(_Dto):
    project_id: str
    trace_id: str
    start_ns: int
    end_ns: int | None
    duration_ns: int | None
    last_span_ns: int
    status: RunStatus
    root_name: str | None
    agent_name: str | None
    source: SourceRef | None
    source_count: int
    models: list[str]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    usage_source: UsageSource
    cost_usd: Decimal | None
    unpriced: bool
    llm_calls: int
    tool_calls: int
    error_spans: int
    span_count: int
    missing_parents: int
    session_id: str | None
    is_genai: bool

    @property
    def ref(self) -> RunRef:
        return RunRef(project_id=self.project_id, trace_id=self.trace_id)


class TokenCostTotals(_Dto):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    cost_usd: Decimal
    includes_unpriced: bool
    unpriced_calls: int
    usage_source: UsageSource


class MessagePart(_Dto):
    type: Literal["text", "tool_call", "tool_result", "reasoning", "other"]
    content: str | None = None
    id: str | None = None
    name: str | None = None
    arguments_json: str | None = None
    result_json: str | None = None
    raw_type: str | None = None
    raw_json: str | None = None


class Message(_Dto):
    role: Literal["system", "user", "assistant", "tool", "other"]
    name: str | None = None
    finish_reason: str | None = None
    direction: Literal["input", "output"]
    parts: list[MessagePart]


class LLMCallView(_Dto):
    span_id: str
    operation: str
    provider: str | None
    request_model: str | None
    response_model: str | None
    model: str
    params: dict[str, float | int | str | bool | None]
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: Decimal | None
    unpriced: bool
    pricing_version: int | None
    status: OkError
    error_type: str | None
    start_ns: int
    duration_ns: int
    finish_reasons: list[str]
    messages: list[Message]
    content_dropped: bool


class ToolCallView(_Dto):
    span_id: str
    tool_name: str
    tool_call_id: str | None
    tool_type: str | None
    status: OkError
    error_type: str | None
    start_ns: int
    duration_ns: int
    arguments_json: str | None = None
    result_json: str | None = None
    content_dropped: bool


class ApprovalView(_Dto):
    kind: Literal["approval", "deferral", "elicitation"]
    decision: Literal["approved", "rejected", "edited", "timeout", "pending"]
    approval_key: str | None
    approver: str | None
    requested_ns: int
    decided_ns: int | None
    wait_ns: int | None
    tool_call_id: str | None
    tool_name: str | None
    node_name: str | None


class SpanNode(_Dto):
    span_id: str
    parent_span_id: str | None
    depth: int
    missing_parent: bool
    placeholder: bool = False
    name: str
    label: str
    kind: SpanKind
    is_genai: bool
    start_ns: int
    end_ns: int
    duration_ns: int
    status: Literal["unset", "ok", "error"]
    status_message: str | None
    source: SourceRef | None
    agent_name: str | None
    llm: LLMCallView | None
    tool: ToolCallView | None
    approval: ApprovalView | None
    subtree_tokens: int
    subtree_cost_usd: Decimal
    subtree_includes_unpriced: bool
    truncated: bool
    redacted: bool
    content_dropped: bool


class GraphNode(_Dto):
    id: str
    label: str
    kind: Literal["agent", "node", "tool", "llm", "approval"]
    executions: int
    status: OkError
    interrupted: bool
    span_ids: list[str]


class GraphEdge(_Dto):
    source: str
    target: str
    kind: Literal["parent", "sequence"]
    count: int
    taken: bool = True


class AgentGraph(_Dto):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    collapsed: bool
    fallback_sequence: bool
    total_executions: int


class TraceDetail(_Dto):
    run: RunSummary
    spans: list[SpanNode]
    span_offset: int
    next_offset: int | None
    graph: AgentGraph | None
    totals: TokenCostTotals
    llm_calls: list[LLMCallView]


class SpanEventRaw(_Dto):
    name: str
    time_ns: int
    attributes: dict[str, JsonValue]


class SpanLinkRaw(_Dto):
    trace_id: str
    span_id: str
    attributes: dict[str, JsonValue]


class SpanRaw(_Dto):
    span_id: str
    attributes: dict[str, JsonValue]
    resource: dict[str, JsonValue]
    scope_name: str | None
    scope_version: str | None
    events: list[SpanEventRaw]
    links: list[SpanLinkRaw]
    dropped_keys: list[str]


class ErrorInfo(_Dto):
    message: str | None
    exception_type: str | None
    stacktrace: str | None


class SpanDetail(_Dto):
    span: SpanNode
    llm: LLMCallView | None
    tool: ToolCallView | None
    error: ErrorInfo | None
    raw: SpanRaw


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


class SessionSummary(_Dto):
    project_id: str
    session_id: str
    external_id: str
    first_ns: int
    last_ns: int
    run_count: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    cost_usd: Decimal
    includes_unpriced: bool


class SessionRunView(_Dto):
    run: RunSummary
    cumulative_tokens: int
    cumulative_cost_usd: Decimal
    cumulative_includes_unpriced: bool


class SessionDetail(_Dto):
    session: SessionSummary
    runs: list[SessionRunView]


# ── dashboard ────────────────────────────────────────────────────────────────────────────────


class Kpis(_Dto):
    runs: int
    error_runs: int
    error_rate: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: Decimal
    unpriced_runs: int
    includes_unpriced: bool
    p95_duration_ns: int | None
    active_sessions: int


class SeriesPoint(_Dto):
    bucket_ns: int
    group: str
    value: int


class TimeSeries(_Dto):
    bucket_ns: int
    groups: list[str]
    points: list[SeriesPoint]


class ModelUsageRow(_Dto):
    model: str
    bucket_ns: int | None
    calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: Decimal
    unpriced_calls: int
    includes_unpriced: bool


class ModelUsage(_Dto):
    bucket_ns: int
    totals: list[ModelUsageRow]
    over_time: list[ModelUsageRow]


class LatencyRow(_Dto):
    group: str
    count: int
    p50_ns: int
    p95_ns: int
    p99_ns: int


class ToolStat(_Dto):
    tool_name: str
    calls: int
    successes: int
    errors: int
    error_rate: float


class SourceUsage(_Dto):
    source: SourceRef
    runs: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    unpriced_runs: int
    includes_unpriced: bool


class ApprovalDecisionCount(_Dto):
    decision: str
    count: int
    median_wait_ns: int | None


class ApprovalStats(_Dto):
    by_decision: list[ApprovalDecisionCount]
    median_wait_ns: int | None


class DashboardData(_Dto):
    project_ids: list[str]
    time_from_ns: int
    time_to_ns: int
    bucket_ns: int
    kpis: Kpis | None = None
    runs_over_time: TimeSeries | None = None
    model_usage: ModelUsage | None = None
    latency_by_operation: list[LatencyRow] | None = None
    latency_by_node: list[LatencyRow] | None = None
    tools: list[ToolStat] | None = None
    by_source: list[SourceUsage] | None = None
    approvals: ApprovalStats | None = None
    metric_tokens: TimeSeries | None = None
    failed: list[str] = []


# ── selectors, live updates, storage ─────────────────────────────────────────────────────────


class ProjectRef(_Dto):
    id: str
    slug: str
    name: str


class SourceView(_Dto):
    project_id: str
    source: SourceRef
    first_seen_ns: int
    last_seen_ns: int


class ChangeNotice(_Dto):
    project_ids: list[str]
    run_keys: list[RunRef]
    truncated: bool
    kinds: list[ChangeKind]


class PurgeView(_Dto):
    trigger: Literal["scheduled", "manual"]
    started_ns: int
    finished_ns: int | None
    deleted_traces: int
    deleted_bytes_est: int
    error_class: str | None


class StorageStatus(_Dto):
    estimated_bytes: int
    physical_bytes: int | None
    size_limit_bytes: int | None
    runs: int
    spans: int
    last_purge: PurgeView | None


# ── admin ────────────────────────────────────────────────────────────────────────────────────


class Project(_Dto):
    id: str
    slug: str
    name: str
    content_storage: bool
    retention_days: int
    request_rate: int
    request_burst: int
    span_rate: int
    span_burst: int
    settings_version: int
    created_at_ns: int
    updated_at_ns: int
    updated_by: str | None


class ProjectSettingsPatch(_Dto):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    content_storage: bool | None = None
    retention_days: int | None = Field(default=None, ge=0, le=3650)
    request_rate: int | None = Field(default=None, ge=1)
    request_burst: int | None = Field(default=None, ge=1)
    span_rate: int | None = Field(default=None, ge=1)
    span_burst: int | None = Field(default=None, ge=1)


class ApiKeyView(_Dto):
    key_id: str
    prefix: str
    label: str
    created_at_ns: int
    created_by: str | None
    last_used_at_ns: int | None
    revoked_at_ns: int | None
    revoked: bool


class CreatedApiKey(_Dto):
    key_id: str
    plaintext: SecretStr
    snippet: str


class PricingEntryView(_Dto):
    provider: str = ""
    model_pattern: str = Field(min_length=1, max_length=200)
    match_kind: Literal["exact", "glob"]
    input_usd_per_mtok: Decimal = Field(ge=0)
    output_usd_per_mtok: Decimal = Field(ge=0)
    cached_input_usd_per_mtok: Decimal | None = Field(default=None, ge=0)
    cache_write_usd_per_mtok: Decimal | None = Field(default=None, ge=0)


class PricingVersionInfo(_Dto):
    version: int
    effective_from_ns: int
    source: Literal["bundled", "editor", "csv"]
    note: str | None
    created_at_ns: int
    created_by: str | None


class PricingTable(_Dto):
    info: PricingVersionInfo
    entries: list[PricingEntryView]


class UnpricedModel(_Dto):
    provider: str
    model: str
    calls: int
    last_seen_ns: int


class PatternRule(_Dto):
    name: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    regex: str = Field(min_length=1)
    enabled: bool = True


class FieldRule(_Dto):
    key_glob: str = Field(min_length=1, max_length=256)
    action: Literal["drop", "mask"]


class RedactionRuleSet(_Dto):
    presets: dict[Literal["secrets", "email", "phone", "credit_card"], bool]
    pattern_rules: list[PatternRule] = []
    field_rules: list[FieldRule] = []


class SavedFilterView(_Dto):
    id: str
    project_id: str
    name: str
    query: str
    created_by: str | None
    created_at_ns: int
    updated_at_ns: int
