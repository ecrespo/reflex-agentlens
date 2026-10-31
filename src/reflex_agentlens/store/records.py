"""Storage records: frozen dataclasses exchanged across the ``StorageBackend`` protocol (P2-01.2).

API §5 names these types; ``store`` cannot import ``query``, so they live here with
**storage-level** fields (Tasks P2 design note): OTel IDs as ``bytes``, public IDs as ULID
strings, money as ``int`` nano-USD, times as ``int`` Unix ns. ``query.dto`` turns them into the
Pydantic DTOs of API §6 (Data Model §8 conversions). Internal ``pk`` values never appear here.

Scope of this module today: one record per table row that crosses the protocol, the ingest
write path (API §5.1), the settings surface and the change feed. Read and aggregate result types
(``RunSummary``, ``TraceDetail``, ``Kpis``, …) are added by the tasks that implement those
methods (P2-07.x, P2-08.x), so each lands with its query and contract test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from reflex_agentlens.store.attrs import JsonValue

__all__ = [
    "CHANGE_EVENT_MAX_RUN_KEYS",
    "ApiKeyRecord",
    "ApprovalDecision",
    "ApprovalKind",
    "ApprovalRow",
    "ChangeEvent",
    "ChangeKind",
    "GraphNodeRow",
    "IngestSettings",
    "JsonObject",
    "LLMCallRow",
    "MetricKind",
    "MetricPoint",
    "NewApiKeyRecord",
    "NewProject",
    "NewSavedFilter",
    "NormalizedBatch",
    "NormalizedSpan",
    "OkError",
    "PricingEntry",
    "PricingSource",
    "PricingVersion",
    "PricingVersionMeta",
    "Project",
    "ProjectPatch",
    "ProjectScope",
    "PurgeReport",
    "PurgeTrigger",
    "RedactionRuleSet",
    "ResourceIn",
    "RunKey",
    "RunStatus",
    "SavedFilter",
    "SchemaStatus",
    "SessionKey",
    "SourceKey",
    "SpanCategory",
    "SpanRaw",
    "TimeRange",
    "ToolCallRow",
    "WriteResult",
]

OkError = Literal["ok", "error"]
RunStatus = Literal["in_progress", "ok", "error", "incomplete"]
SpanCategory = Literal["llm", "tool", "agent", "approval", "generic_genai", "generic"]
ApprovalKind = Literal["approval", "deferral", "elicitation"]
ApprovalDecision = Literal["approved", "rejected", "edited", "timeout", "pending"]
PricingSource = Literal["bundled", "editor", "csv"]
MetricKind = Literal["sum", "gauge", "histogram"]
PurgeTrigger = Literal["scheduled", "manual"]
ChangeKind = Literal["spans_committed", "runs_finalized", "purged", "settings_changed"]

JsonObject = dict[str, JsonValue]


class SchemaStatus(StrEnum):
    """Database schema vs. the package's migration head (REQ-074)."""

    CURRENT = "current"
    OUTDATED = "outdated"
    NEWER = "newer"


# ── keys and scopes ──────────────────────────────────────────────────────────────────────────

ProjectScope = frozenset[str]
"""Project IDs (ULIDs) a read is restricted to; reads are never unscoped (API §5)."""


@dataclass(frozen=True, slots=True, order=True)
class RunKey:
    """A Run's natural key. Ordering matches the DD-004 lock order within one Project."""

    project_id: str
    trace_id: bytes


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Half-open ``[start_ns, end_ns)``."""

    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        if self.end_ns < self.start_ns:
            raise ValueError("TimeRange end_ns precedes start_ns")


@dataclass(frozen=True, slots=True)
class SourceKey:
    """Natural key of a Source within a Project (REQ-031.a)."""

    service_name: str
    service_namespace: str = ""


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Natural key of a Session within a Project (REQ-036.a); ``external_id`` post-redaction."""

    external_id: str


# ── settings (Data Model §2.2-2.5, §2.17) ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    slug: str
    name: str
    content_storage: bool
    retention_max_age_s: int
    request_rate: int
    request_burst: int
    span_rate: int
    span_burst: int
    settings_version: int
    created_at_ns: int
    updated_at_ns: int
    updated_by: str | None


@dataclass(frozen=True, slots=True)
class NewProject:
    id: str
    slug: str
    name: str
    content_storage: bool = True
    retention_max_age_s: int = 2_592_000
    created_by: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectPatch:
    """Fields left as ``None`` are unchanged; every applied patch bumps ``settings_version``."""

    name: str | None = None
    content_storage: bool | None = None
    retention_max_age_s: int | None = None
    request_rate: int | None = None
    request_burst: int | None = None
    span_rate: int | None = None
    span_burst: int | None = None
    updated_by: str | None = None


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    key_id: str
    project_id: str
    label: str
    salt: bytes
    secret_hash: bytes
    created_at_ns: int
    created_by: str | None
    last_used_at_ns: int | None
    revoked_at_ns: int | None

    def __repr__(self) -> str:  # the hash is not a secret, but keep it out of logs anyway
        return f"ApiKeyRecord(key_id={self.key_id!r}, project_id={self.project_id!r}, …)"


@dataclass(frozen=True, slots=True)
class NewApiKeyRecord:
    key_id: str
    project_id: str
    salt: bytes
    secret_hash: bytes
    created_at_ns: int
    label: str = ""
    created_by: str | None = None

    def __repr__(self) -> str:
        return f"NewApiKeyRecord(key_id={self.key_id!r}, project_id={self.project_id!r}, …)"


@dataclass(frozen=True, slots=True)
class RedactionRuleSet:
    """Stored rule set (Data Model §2.4); the rule grammar is validated by ``ingest.redaction``."""

    presets: JsonObject
    pattern_rules: list[JsonValue]
    field_rules: list[JsonValue]
    updated_at_ns: int = 0
    updated_by: str | None = None


@dataclass(frozen=True, slots=True)
class IngestSettings:
    """What ingest caches per Project (dropped on ``settings_changed``, REQ-006.c)."""

    project_id: str
    content_storage: bool
    request_rate: int
    request_burst: int
    span_rate: int
    span_burst: int
    settings_version: int
    redaction: RedactionRuleSet


@dataclass(frozen=True, slots=True)
class PricingEntry:
    model_pattern: str
    match_kind: Literal["exact", "glob"]
    input_nusd_per_mtok: int
    output_nusd_per_mtok: int
    provider: str = ""
    cached_input_nusd_per_mtok: int | None = None
    cache_write_nusd_per_mtok: int | None = None


@dataclass(frozen=True, slots=True)
class PricingVersionMeta:
    version: int
    effective_from_ns: int
    source: PricingSource
    note: str | None
    created_at_ns: int
    created_by: str | None


@dataclass(frozen=True, slots=True)
class PricingVersion:
    meta: PricingVersionMeta
    entries: tuple[PricingEntry, ...]


@dataclass(frozen=True, slots=True)
class SavedFilter:
    id: str
    project_id: str
    name: str
    query: str
    created_by: str | None
    created_at_ns: int
    updated_at_ns: int


@dataclass(frozen=True, slots=True)
class NewSavedFilter:
    id: str
    project_id: str
    name: str
    query: str
    created_by: str | None = None


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """One ``purge_log`` row (Data Model §2.18)."""

    trigger: PurgeTrigger
    started_ns: int
    finished_ns: int | None
    deleted_traces_age: int
    deleted_traces_size: int
    deleted_bytes_est: int
    error_class: str | None = None


# ── ingest write path (API §5.1) ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResourceIn:
    """A resource as written: ``attrs_hash`` = BLAKE2b-128 of the canonical attributes."""

    attrs_hash: bytes
    attributes: JsonObject
    schema_url: str | None = None


@dataclass(frozen=True, slots=True)
class LLMCallRow:
    """Per-span LLM call derivation (Data Model §2.11); keys are filled by the backend."""

    start_ns: int
    duration_ns: int
    operation: str
    unpriced: bool
    provider: str | None = None
    request_model: str | None = None
    response_model: str | None = None
    model: str = "unknown"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_nusd: int | None = None
    pricing_version: int | None = None
    status: OkError = "ok"
    error_type: str | None = None
    finish_reasons: list[str] = field(default_factory=list)
    params: JsonObject = field(default_factory=dict)
    content_dropped: bool = False


@dataclass(frozen=True, slots=True)
class ToolCallRow:
    """Data Model §2.12."""

    start_ns: int
    duration_ns: int
    status: OkError
    tool_name: str = "unknown"
    tool_call_id: str | None = None
    tool_type: str | None = None
    error_type: str | None = None
    content_dropped: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    """Data Model §2.13."""

    kind: ApprovalKind
    decision: ApprovalDecision
    requested_ns: int
    approval_key: str | None = None
    approver: str | None = None
    decided_ns: int | None = None
    wait_ns: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    node_name: str | None = None


@dataclass(frozen=True, slots=True)
class GraphNodeRow:
    """Data Model §2.14."""

    node_key: str
    node_name: str
    node_kind: Literal["agent", "workflow", "node"]
    start_ns: int
    end_ns: int
    status: OkError
    explicit_parent_key: str | None = None
    step: int | None = None
    agent_name: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedSpan:
    """One span after normalization, redaction and per-span derivation (Data Model §2.10)."""

    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes | None
    source: SourceKey
    resource: ResourceIn
    name: str
    start_ns: int
    end_ns: int
    mapping_version: int
    size_bytes: int
    received_at_ns: int
    otel_kind: int = 0
    category: SpanCategory = "generic"
    is_genai: bool = False
    status_code: int = 0
    status_message: str | None = None
    scope_name: str | None = None
    scope_version: str | None = None
    attributes: JsonObject = field(default_factory=dict)
    events: list[JsonValue] = field(default_factory=list)
    links: list[JsonValue] = field(default_factory=list)
    dropped_counts: JsonObject = field(default_factory=dict)
    dropped_keys: list[str] = field(default_factory=list)
    agent_name: str | None = None
    session: SessionKey | None = None
    agent_usage_input: int | None = None
    agent_usage_output: int | None = None
    flags: int = 0
    llm_call: LLMCallRow | None = None
    tool_call: ToolCallRow | None = None
    approval: ApprovalRow | None = None
    graph_node: GraphNodeRow | None = None
    search_doc: str | None = None

    def __post_init__(self) -> None:
        if len(self.trace_id) != 16 or len(self.span_id) != 8:
            raise ValueError("trace_id must be 16 bytes and span_id 8 bytes")
        if self.parent_span_id is not None and len(self.parent_span_id) != 8:
            raise ValueError("parent_span_id must be 8 bytes")
        if self.end_ns < self.start_ns:
            raise ValueError("span end_ns precedes start_ns")


@dataclass(frozen=True, slots=True)
class NormalizedBatch:
    project_id: str
    spans: tuple[NormalizedSpan, ...]


@dataclass(frozen=True, slots=True)
class WriteResult:
    """API §5.1."""

    inserted: int
    duplicates: int
    affected_runs: list[RunKey]
    committed_at_ns: int


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """Data Model §2.16 (one data point of an OTLP metric)."""

    project_id: str
    source: SourceKey
    name: str
    kind: MetricKind
    start_ns: int
    time_ns: int
    attrs_hash: bytes
    attributes: JsonObject
    unit: str | None = None
    is_monotonic: bool | None = None
    temporality: int | None = None
    model: str | None = None
    token_type: str | None = None
    value_int: int | None = None
    value_double: float | None = None
    count: int | None = None
    sum: float | None = None
    bucket_counts: list[int] | None = None
    explicit_bounds: list[float] | None = None


# ── reads defined by the API itself ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SpanRaw:
    """Raw view of one span (API v1.2 §6, Data Model Q5b, REQ-151.e)."""

    span_id: bytes
    attributes: JsonObject
    resource: JsonObject
    scope_name: str | None
    scope_version: str | None
    events: list[JsonValue]
    links: list[JsonValue]
    dropped_keys: list[str]


# ── change feed (API §5.2) ───────────────────────────────────────────────────────────────────

CHANGE_EVENT_MAX_RUN_KEYS = 200


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    kind: ChangeKind
    project_id: str
    run_keys: tuple[RunKey, ...]
    truncated: bool
    committed_at_ns: int

    @classmethod
    def build(
        cls, kind: ChangeKind, project_id: str, run_keys: list[RunKey], committed_at_ns: int
    ) -> ChangeEvent:
        """Cap ``run_keys`` at 200 and set ``truncated`` beyond (API §5.2)."""
        return cls(
            kind=kind,
            project_id=project_id,
            run_keys=tuple(run_keys[:CHANGE_EVENT_MAX_RUN_KEYS]),
            truncated=len(run_keys) > CHANGE_EVENT_MAX_RUN_KEYS,
            committed_at_ns=committed_at_ns,
        )
