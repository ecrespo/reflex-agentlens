# reflex-agentlens — API Specification v1

## Metadata

| Field | Value |
|---|---|
| **Author** | Ernesto Crespo (`ecrespo`) |
| **Status** | `APPROVED` |
| **API version** | v1.1 (ships with package 0.1.0) |
| **Date** | 2026-10-30 |
| **Related PRD** | [`specs/prd/agentlens-v0.1.md`](../prd/agentlens-v0.1.md) v1.1 |
| **Governed by** | [`specs/constitution.md`](../constitution.md) v1.0 |
| **Base URLs** | `serve`: `http://<host>:8000` (UI + OTLP/HTTP), gRPC `<host>:4317` · `collect`: OTLP/HTTP `http://<host>:4318`, gRPC `<host>:4317` |

---

## 1. Overview

agentlens exposes six contract surfaces. They are versioned together under "API v1":

| # | Surface | Consumers | Transport | §  |
|---|---|---|---|---|
| A | **OTLP ingest** (traces, metrics; logs → unimplemented) | Any OTel exporter | HTTP/1.1+2 (protobuf, JSON), gRPC | 3 |
| B | **Operational HTTP** (health, standalone auth) | Orchestrators, browsers | HTTP | 4 |
| C | **Storage protocol** (`reflex_agentlens.store`) | `ingest`, `query`; third-party backends (provisional) | In-process Python, async | 5 |
| D | **Query & admin API** (`reflex_agentlens.query`) | `pages`, `components`, host apps | In-process Python, async | 6 |
| E | **Embedding API** (`reflex_agentlens`, `.pages`) plus page URL contract | Host Reflex apps, `site/` | Python + URLs | 7 |
| F | **Configuration & CLI** | Operators | env, TOML, CLI | 8 |

**No public REST query API in v0.1.** Pages call the query layer in-process (Constitution
Art. 6: pages → query → store). A JSON read API is a v0.2 candidate. The query DTOs below are
designed so that it can be a thin HTTP wrapper over them.

### 1.1 Layering and import contract (normative)

```
reflex_agentlens.pages / .components / .app (Reflex)
        │  imports only
        ▼
reflex_agentlens.query      (QueryService, AdminService, DTOs)   ── no reflex import
        │
        ▼
reflex_agentlens.store      (StorageBackend protocol, SQLite/Postgres impls, ChangeFeed)
        ▲
        │
reflex_agentlens.ingest     (OTLP receivers, normalizer, redaction, cost)  ── no reflex import
        │
reflex_agentlens.core       (config model, security primitives, ids, errors, semconv constants) ── leaf
```

`ingest` SHALL NOT import `query`. `query` SHALL NOT import `ingest`, except
`ingest.redaction.preview`, which is a pure function used by `AdminService.preview_redaction`.
This exception is listed in the import contract. `core` imports nothing from the other packages.

### 1.2 Requirement traceability

Every endpoint, method and setting below cites the PRD requirement(s) it realizes in
`[REQ-…]` brackets. Anything without a citation is supporting machinery and is listed in §10.

---

## 2. Identifiers and shared conventions

| Identifier | Format | Notes |
|---|---|---|
| `trace_id` | 32 lowercase hex chars (16 bytes) | From OTLP. All-zero is invalid [REQ-010] |
| `span_id` | 16 lowercase hex chars (8 bytes) | From OTLP. All-zero is invalid |
| Run identity | (`project_id`, `trace_id`) | One Run per trace per Project [REQ-032.a] |
| `project_id` | ULID string (26 chars) | Internal, opaque |
| `project.slug` | `^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$`, unique | Used in URLs and the UI |
| `source_id` | ULID | Natural key (`project_id`, `service_namespace`, `service_name`) [REQ-031] |
| `session_id` | ULID | Natural key (`project_id`, `external_id`). `external_id` = conversation ID as sent [REQ-036] |
| `api_key_id` | 8 chars, base32 Crockford lowercase | Public part of the key, see §3.4 |
| `pricing_version` | positive integer, monotonically increasing | [REQ-060] |
| Timestamps (storage, DTOs) | `int` Unix **nanoseconds**, UTC | [Constitution Art. 5] |
| Timestamps (URLs, config) | RFC 3339 (`2026-10-30T12:00:00Z`) or relative (`now-24h`) | Parsed in `query` |
| Money | `decimal.Decimal`, USD, serialized as string in JSON | Never float [Art. 5] |
| Durations (config) | `"500ms"`, `"30s"`, `"5m"`, `"1h"`, `"30d"` or integer seconds | |
| Sizes (config) | `"256KiB"`, `"8MiB"`, `"1GiB"` or integer bytes | |

**Error hygiene (all surfaces).** Error messages and log lines carry IDs, counts, sizes, field
names and error classes only. They never carry attribute values, message content, or keys
[REQ-323].

---

## 3. Surface A — OTLP ingest

Conforms to the OTLP specification (protocol version pinned in the Technical Design) for
`traces` and `metrics`. Only the agentlens-specific behavior is specified here. Everything else
follows OTLP.

### 3.1 Endpoints

| Signal | OTLP/HTTP | OTLP/gRPC | Behavior | REQ |
|---|---|---|---|---|
| Traces | `POST {ingest_prefix}/v1/traces` | `opentelemetry.proto.collector.trace.v1.TraceService/Export` | Ingest | 001–004, 009, 010 |
| Metrics | `POST {ingest_prefix}/v1/metrics` | `opentelemetry.proto.collector.metrics.v1.MetricsService/Export` | Ingest `gen_ai.*` only | 005 |
| Logs | `POST {ingest_prefix}/v1/logs` | `opentelemetry.proto.collector.logs.v1.LogsService/Export` | HTTP **404**, gRPC **UNIMPLEMENTED** (no auth check) | 008 |

- `ingest_prefix` defaults to `""` in `serve`/`collect`, so `OTEL_EXPORTER_OTLP_ENDPOINT=http://host:8000`
  works unmodified. When embedded it defaults to `/agentlens/otlp` (§7.2).
- HTTP/2 cleartext (h2c) is not required. HTTP/1.1 is sufficient for OTLP/HTTP.

### 3.2 Request rules (HTTP)

| Header | Values | Required | On violation |
|---|---|---|---|
| `Content-Type` | `application/x-protobuf` \| `application/json` (parameters such as `; charset=utf-8` ignored) | Yes | **415** `UNSUPPORTED_MEDIA_TYPE` |
| `Content-Encoding` | absent \| `gzip` | No | **415** for any other value |
| `Authorization` | `Bearer <api-key>` | One of the two | **401** |
| `X-Agentlens-Key` | `<api-key>` (for setups where `Authorization` is consumed by a proxy) | One of the two | **401** |
| `Content-Length` | > `ingest.max_body_bytes` | — | **413** before the body is read [REQ-007.b] |

If both key headers are present, `X-Agentlens-Key` wins. Exporter configuration:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:8000"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"         # or grpc (→ :4317), http/json
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20al_k7m2q9xd_3Jc…"
```

**JSON encoding** follows the OTLP JSON mapping: lowerCamelCase field names, `traceId`/`spanId`
as hex strings (not base64), enums as integers, 64-bit integers as strings or numbers
[REQ-002.a]. Responses use the request's encoding [REQ-002.b].

### 3.3 Responses

**Success (HTTP 200 / gRPC OK).** The response is sent only after the commit [REQ-009.a].

```json
{}                                                     // all spans accepted
{ "partialSuccess": { "rejectedSpans": "3",
                      "errorMessage": "3 spans rejected: invalid_span_id=2, end_before_start=1" } }
```

For metrics, `partialSuccess.rejectedDataPoints` counts only **invalid** GenAI data points.
Discarded non-GenAI metrics are *not* rejections, so they are not reported there [REQ-005.b].

**Failure.** HTTP body is a `google.rpc.Status` in the request encoding. gRPC uses the status code plus details.

| Condition | HTTP | gRPC | Retryable | `Retry-After` / `RetryInfo` | REQ |
|---|---|---|---|---|---|
| Undecodable payload | 400 | `INVALID_ARGUMENT` | No | — | 001.c |
| Missing / unknown / revoked key | 401 | `UNAUTHENTICATED` | No | — | 006.b |
| Body > `max_body_bytes` or decompressed > `max_decompressed_bytes` | 413 | `RESOURCE_EXHAUSTED` (no `RetryInfo`) | No | — | 007.b/c |
| Unsupported media type / encoding | 415 | n/a | No | — | 325 |
| Project rate limit exceeded | 429 | `RESOURCE_EXHAUSTED` + `RetryInfo` | Yes | seconds until a token is available (≥ 1) | 007.a |
| Write pipeline saturated / commit latency over threshold | 503 | `UNAVAILABLE` | Yes | `ingest.overload_retry_after` (default 2 s) | 009.b |
| Database unreachable | 503 | `UNAVAILABLE` | Yes | 5 s | 330 |
| Shutting down | 503 | `UNAVAILABLE` | Yes | 1 s | 331 |
| Demo mode | 403 | `PERMISSION_DENIED` | No | — | 231.b |

`google.rpc.Status.message` is one of a fixed set of messages (e.g. `"invalid api key"`,
`"rate limit exceeded"`). It never echoes request content.

**Order of checks** (cheapest first; nothing is persisted from a request that fails any check):
size header → media type → authentication → rate limit (request bucket) → body read and
decompress (with limits) → decode → rate limit (span bucket, counted on decoded spans) →
validation per span → enqueue (backpressure) → commit → respond.

### 3.4 API keys

```
al_<key_id>_<secret>
   │        └─ 32 chars base62 from a CSPRNG (≈190 bits)        [REQ-320.b]
   └─ 8 chars base32 Crockford lowercase; public; stored plaintext as the lookup handle
example: al_k7m2q9xd_3JcV0pXwQ8rT1nZs6bLk2yHe9GdF4aMu
```

- The prefix `al_` lets secret scanners recognize keys. Gitleaks/GitHub custom patterns are documented [REQ-320.b].
- Stored: `key_id`, `hash(secret)` (algorithm per Technical Design Q10), `label`, `created_at`, `last_used_at`
  (updated at most once per minute), `revoked_at` [REQ-320.a].
- Verification: parse → look up by `key_id` → constant-time hash comparison. A positive
  verification cache per process is allowed, with a TTL ≤ 5 s, which bounds revocation latency [REQ-006.c].
- Lifecycle:

```
 create (plaintext shown once) ──▶ ACTIVE ──revoke──▶ REVOKED (terminal; row kept for audit)
                                     │
                         project deleted ─▶ (row deleted with project)
```

### 3.5 Limits (defaults; all configurable, see §8)

| Limit | Default | Scope | REQ |
|---|---|---|---|
| Request rate | 50 req/s, burst 100 | per Project | 007.a |
| Span rate | 20,000 spans/s, burst 40,000 | per Project | 007.a |
| Compressed body | 8 MiB | per request | 007.b |
| Decompressed body | 32 MiB | per request | 007.c |
| gRPC max receive message | = decompressed body limit | per message | 007.b |
| Attribute string value | 256 KiB (truncate + marker) | per value | 041 |
| Ingest queue memory | 256 MiB | per process | 009.c |
| Commit-latency overload threshold | 2 s (p50 over the last 5 s) | per process | 009.b |
| Regex budget | 25 ms per rule per value (fail closed) | per value | 039.d |

Rate-limit state is **per process** (no shared store, Constitution Art. 2). With N `collect`
replicas, the effective Project limit is up to N× the configured value. This is documented as a
known limitation in v0.1.

### 3.6 Span validation (→ `partialSuccess`)

| Rule | Rejection reason key |
|---|---|
| `trace_id` not 16 bytes or all zero | `invalid_trace_id` |
| `span_id` not 8 bytes or all zero | `invalid_span_id` |
| `parent_span_id` present but not 8 bytes | `invalid_parent_span_id` |
| `end_time_unix_nano` < `start_time_unix_nano` | `end_before_start` |
| `start_time_unix_nano` == 0 | `missing_start_time` |
| Redaction hook raised | `redaction_hook_error` [REQ-039.e] |

An in-progress span with `end_time_unix_nano == 0` is **invalid** in OTLP export semantics and
is rejected as `end_before_start`. Exporters only export ended spans.

---

## 4. Surface B — Operational HTTP

| Method & path | Auth | Response | REQ |
|---|---|---|---|
| `GET /healthz` | none | `200 {"status":"ok"}`. `503 {"status":"shutting_down"}` during graceful shutdown | 340 |
| `GET /readyz` | none | `200 {"status":"ready"}` or `503 {"status":"not_ready","checks":{"database":"fail","schema":"ok","ingest":"ok"}}`. No versions, no config | 340, 330 |
| `POST /_agentlens/auth/login` | none (form) | See below | 224, 321 |
| `POST /_agentlens/auth/logout` | session cookie | `303` → login page. Clears the cookie | 224 |

Health endpoints are served by `serve` and `collect` on the HTTP port. In embedded mode they are
mounted only if the host opts in (`mount(..., health=True)`).

**Login (`password` mode only; 404 in other modes).**

```http
POST /_agentlens/auth/login
Content-Type: application/x-www-form-urlencoded
Origin: https://agentlens.example.org

username=admin&password=…&next=%2Fagentlens%2Fruns
```

| Outcome | Response |
|---|---|
| Valid | `303 See Other`, `Location: <next>` (only same-origin relative paths accepted, otherwise `/`). `Set-Cookie: agentlens_session=<signed>; HttpOnly; SameSite=Lax; Path=/; Secure (if HTTPS); Max-Age=<auth.session_ttl>` |
| Invalid credentials | `303` → `/login?error=1` (no hint whether the user or the password was wrong) |
| `Origin`/`Referer` host ≠ request host | `403` (CSRF guard) |
| > 5 failures/min from the client IP | Response delayed ≥ 1 s per further attempt [REQ-224.d] |

The cookie payload is `{sub, iat, exp}` signed with `auth.session_secret` (HMAC). Reflex state
reads it from the websocket connection headers (`router.headers.cookie`). That is how the
standalone auth hook (§7.3) authenticates events.

---

## 5. Surface C — Storage protocol (`reflex_agentlens.store`)

The protocol is **async** and fully typed (`mypy --strict`). Status: **provisional** for
third-party implementations until v1.0. Internal callers rely on it as stable within v0.x.
Every backend SHALL pass `tests/contract/` [REQ-070.b]. Entity field definitions live in the
Data Model. Here only the names and method semantics are fixed.

```python
class StorageBackend(Protocol):
    # ── lifecycle ─────────────────────────────────────────────────────────
    async def open(self) -> None: ...                       # connect pools; no implicit migration
    async def close(self) -> None: ...
    async def ping(self) -> bool: ...                       # [REQ-340, REQ-330]
    async def schema_status(self) -> SchemaStatus: ...      # CURRENT | OUTDATED | NEWER  [REQ-074]
    async def migrate(self) -> None: ...                    # Alembic upgrade head, under a lock [REQ-074.d]

    # ── ingest path ───────────────────────────────────────────────────────
    async def write_batch(self, batch: NormalizedBatch) -> WriteResult: ...
    async def write_metric_points(self, points: Sequence[MetricPoint]) -> int: ...
    async def resolve_api_key(self, key_id: str) -> ApiKeyRecord | None: ...
    async def touch_api_key(self, key_id: str, at_ns: int) -> None: ...
    async def get_ingest_settings(self, project_id: str) -> IngestSettings: ...  # redaction, content toggle, limits
    async def get_pricing(self, at_ns: int | None = None) -> PricingVersion: ...

    # ── derived views ─────────────────────────────────────────────────────
    async def finalize_stale_runs(self, older_than_ns: int, limit: int) -> list[RunKey]: ...  # in_progress → incomplete
    async def rebuild(self, scope: RebuildScope, deriver: SpanDeriver,
                      pricing: PricingVersion) -> RebuildReport: ...                     # [REQ-042]
    async def recompute_costs(self, project_id: str, time_range: TimeRange,
                              pricing: PricingVersion) -> int: ...                       # [REQ-062]

    # ── reads (all filtered by project_ids; never unscoped) ───────────────
    async def list_runs(self, q: RunQuery) -> Page[RunSummary]: ...
    async def find_runs_by_trace_id(self, project_ids: ProjectScope, trace_id: str) -> list[RunKey]: ...
    async def get_trace(self, key: RunKey) -> TraceDetail | None: ...
    async def list_llm_calls(self, key: RunKey) -> list[LLMCallView]: ...
    async def list_sessions(self, q: SessionQuery) -> Page[SessionSummary]: ...
    async def get_session(self, project_ids: ProjectScope, session_id: str) -> SessionDetail | None: ...
    async def list_sources(self, project_ids: ProjectScope) -> list[SourceView]: ...
    async def distinct_values(self, project_ids: ProjectScope, field: FacetField,
                              time_range: TimeRange, limit: int) -> list[str]: ...       # models, tools for filter UIs

    # ── aggregates (SQL-only; bounded rows) [REQ-106.h] ───────────────────
    async def agg_kpis(self, q: AggQuery) -> Kpis: ...                                   # [REQ-106.g]
    async def agg_runs_over_time(self, q: AggQuery) -> TimeSeries: ...                   # [REQ-106.a]
    async def agg_tokens_cost_by_model(self, q: AggQuery) -> ModelUsage: ...             # [REQ-106.b]
    async def agg_latency_percentiles(self, q: AggQuery, by: LatencyDim) -> list[LatencyRow]: ...  # [REQ-106.c]
    async def agg_tool_stats(self, q: AggQuery, top_n: int) -> list[ToolStatRow]: ...    # [REQ-106.d]
    async def agg_by_source(self, q: AggQuery) -> list[SourceUsageRow]: ...              # [REQ-106.e]
    async def agg_approvals(self, q: AggQuery) -> ApprovalStats: ...                     # [REQ-106.f]
    async def agg_metric_token_usage(self, q: AggQuery) -> TimeSeries: ...               # [REQ-107]

    # ── settings ──────────────────────────────────────────────────────────
    async def list_projects(self) -> list[Project]: ...
    async def create_project(self, p: NewProject) -> Project: ...
    async def update_project(self, project_id: str, patch: ProjectPatch) -> Project: ...
    async def delete_project(self, project_id: str) -> None: ...                         # cascades all data
    async def create_api_key(self, rec: NewApiKeyRecord) -> ApiKeyRecord: ...
    async def list_api_keys(self, project_id: str) -> list[ApiKeyRecord]: ...
    async def revoke_api_key(self, key_id: str, at_ns: int) -> None: ...
    async def save_pricing(self, entries: Sequence[PricingEntry], author: str) -> PricingVersion: ...
    async def list_pricing_versions(self) -> list[PricingVersionMeta]: ...
    async def list_unpriced_models(self, project_ids: ProjectScope) -> list[UnpricedModel]: ...
    async def save_redaction_rules(self, project_id: str, rules: RedactionRuleSet) -> None: ...
    async def list_saved_filters(self, project_id: str) -> list[SavedFilter]: ...
    async def save_filter(self, f: NewSavedFilter) -> SavedFilter: ...
    async def delete_filter(self, filter_id: str) -> None: ...

    # ── retention ─────────────────────────────────────────────────────────
    async def purge_by_age(self, now_ns: int, batch_traces: int) -> PurgeReport: ...     # [REQ-090]
    async def purge_by_size(self, limit_bytes: int, batch_traces: int) -> PurgeReport: ...  # [REQ-091]
    async def storage_usage(self) -> StorageUsage: ...                                   # [REQ-173]
    async def last_purge(self) -> PurgeReport | None: ...

    # ── coordination ──────────────────────────────────────────────────────
    def job_lock(self, name: JobName) -> AbstractAsyncContextManager[bool]: ...          # True if acquired [REQ-092.b]
    @property
    def change_feed(self) -> ChangeFeed: ...                                             # [REQ-140]
```

### 5.1 `write_batch` semantics (normative)

`NormalizedBatch` = `project_id` + list of `NormalizedSpan` (span row, raw attributes, events,
links, and optional per-span `LLMCallRow` / `ToolCallRow` / `ApprovalRow` / `GraphNodeRow` /
`SearchDoc`), with Sources and Sessions referenced by natural key.

In **one transaction**, the backend SHALL:

1. Upsert Sources and Sessions by natural key (create on first sight) [REQ-031, REQ-036.a].
2. Insert spans with *insert-or-ignore* on (`project_id`, `trace_id`, `span_id`). Ignored rows
   (duplicates) and their per-span derived rows are dropped, and they are counted in
   `WriteResult.duplicates` [REQ-038].
3. Insert per-span derived rows for newly inserted spans only.
4. For every affected `trace_id`, **recompute the Run row from scratch in SQL** from all of its
   stored spans and derived rows (status, totals, counts, session, models). This includes
   re-linking orphans whose parent arrived [REQ-032, REQ-037]. Because recomputation is a pure
   function of the stored set, the result is independent of arrival order and batching.
5. Recompute affected Sessions' aggregates from their Runs [REQ-036.b].
6. Commit, then publish one `ChangeEvent` per Project on the change feed (after the commit, never
   before) [REQ-140.a].

Per-span derivations (LLMCall/ToolCall/Approval/GraphNode, cost, redaction, search document)
happen in `ingest` **before** `write_batch`, because they depend only on the span itself.

```python
@dataclass(frozen=True, slots=True)
class WriteResult:
    inserted: int
    duplicates: int
    affected_runs: list[RunKey]
    committed_at_ns: int
```

### 5.2 Change feed

```python
class ChangeFeed(Protocol):
    async def publish(self, event: ChangeEvent) -> None: ...
    def subscribe(self, project_ids: ProjectScope) -> AsyncIterator[ChangeEvent]: ...

@dataclass(frozen=True, slots=True)
class ChangeEvent:
    kind: Literal["spans_committed", "runs_finalized", "purged", "settings_changed"]
    project_id: str
    run_keys: tuple[RunKey, ...]          # capped at 200; `truncated=True` beyond
    truncated: bool
    committed_at_ns: int
```

Delivery is **at-most-once and lossy by design**. Subscribers treat an event as "something
changed, re-query", never as data. Backends SHALL deliver across processes that share the
database (mechanism: Technical Design Q4). Coalescing to ≤ 1 refresh/s per UI session happens in
the query layer's subscription helper [REQ-140.b].

`settings_changed` also tells ingest processes to drop cached `IngestSettings` and API-key
verification entries [REQ-006.c].

---

## 6. Surface D — Query & admin API (`reflex_agentlens.query`)

The application layer used by pages, and usable by host apps. Every method takes a `Viewer` and
enforces project scoping and admin rights **here**, not in pages [REQ-174, REQ-191.c].
Errors are raised as typed exceptions (§6.6).

```python
@dataclass(frozen=True, slots=True)
class Viewer:
    id: str
    display_name: str
    is_admin: bool
    allowed_project_ids: frozenset[str] | None = None   # None = all projects   [REQ-191.c]
```

DTOs are frozen Pydantic v2 models (JSON-serializable, Reflex-state friendly). Money fields are
`Decimal` and token fields are `int`.

### 6.1 `QueryService` (reads)

| Method | Returns | REQ |
|---|---|---|
| `list_runs(viewer, filter: RunFilter, sort: RunSort = START_DESC, cursor: str \| None = None, limit: int = 50)` | `Page[RunSummary]` | 100, 101 |
| `resolve_trace(viewer, trace_id, project_slug: str \| None = None)` | `list[RunRef]` (0 = not found, >1 = disambiguation) | 103 |
| `get_trace(viewer, run: RunRef)` | `TraceDetail` (raises `NotFound`) | 103 |
| `list_llm_calls(viewer, run: RunRef)` | `list[LLMCallView]`, the stable normalized I/O shape | 104 |
| `list_sessions(viewer, filter: SessionFilter, sort: SessionSort, cursor, limit)` | `Page[SessionSummary]` | 105.a |
| `get_session(viewer, session_id)` | `SessionDetail` | 105.b |
| `dashboard(viewer, scope: AggScope)` | `DashboardData` (all REQ-106 aggregates, fetched concurrently) | 106, 141 |
| `facets(viewer, field: FacetField, scope: AggScope, limit=100)` | `list[str]` (models, tools, statuses, sources) | 145.b |
| `list_projects(viewer)` / `list_sources(viewer, project_ids)` | selectors | 130.a |
| `subscribe(viewer, project_ids, min_interval: float = 1.0)` | `AsyncIterator[ChangeNotice]` (coalesced) | 140 |
| `storage_status(viewer)` | `StorageStatus` (usage, size limit, last purge) | 173 |

**Key DTOs** (field-level types in the Data Model):

```python
class RunSummary(BaseModel):            # [REQ-100.b]
    project_id: str; trace_id: str; start_ns: int; end_ns: int | None; duration_ns: int | None
    status: RunStatus                   # "in_progress" | "ok" | "error" | "incomplete"
    root_name: str | None; agent_name: str | None; source: SourceRef
    models: list[str]; input_tokens: int; output_tokens: int; cached_input_tokens: int
    cost_usd: Decimal | None; unpriced: bool
    llm_calls: int; tool_calls: int; error_spans: int
    session_id: str | None; is_genai: bool

class TraceDetail(BaseModel):           # [REQ-103.a]
    run: RunSummary
    spans: list[SpanNode]               # pre-ordered tree (DFS, siblings by start); orphans under a placeholder
    graph: AgentGraph | None            # [REQ-153]
    totals: TokenCostTotals

class SpanNode(BaseModel):
    span_id: str; parent_span_id: str | None; depth: int; missing_parent: bool
    name: str; label: str               # GenAI-aware label [REQ-150.a]
    kind: SpanKind                      # "llm" | "tool" | "agent" | "approval" | "generic"
    start_ns: int; end_ns: int; status: Literal["unset", "ok", "error"]; status_message: str | None
    llm: LLMCallView | None; tool: ToolCallView | None; approval: ApprovalView | None
    subtree_tokens: int; subtree_cost_usd: Decimal | None
    truncated: bool; redacted: bool
    # raw attributes/events are fetched lazily: get_span_raw(viewer, run, span_id) -> SpanRaw  [REQ-151.e]

class LLMCallView(BaseModel):           # stable v0.2 evals/dataset shape [REQ-104]
    span_id: str; operation: str; provider: str | None
    request_model: str | None; response_model: str | None
    params: dict[str, float | int | str | None]
    input_tokens: int | None; output_tokens: int | None; cached_input_tokens: int | None
    cost_usd: Decimal | None; unpriced: bool; pricing_version: int | None
    finish_reasons: list[str]
    messages: list[Message]; content_dropped: bool        # [REQ-034, REQ-040]

class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    parts: list[MessagePart]            # text | tool_call{id,name,arguments_json} | tool_result{id,content} | other{type,raw_json}
    direction: Literal["input", "output"]

class AgentGraph(BaseModel):            # [REQ-153]
    nodes: list[GraphNode]              # id, label, executions, status, kind ("agent"|"node"|"tool"|"llm"|"approval"), span_ids
    edges: list[GraphEdge]              # source, target, taken: bool, count
    collapsed: bool                     # >150 nodes collapsed [REQ-153.d]
    fallback_sequence: bool             # no graph nodes → tool/LLM sequence [REQ-153.e]
```

`AgentGraph` is data. Mermaid text is produced by `components` from it, so `query` stays free of
rendering code.

### 6.2 `RunFilter` and the Runs URL grammar [REQ-101, REQ-102.b]

```python
class RunFilter(BaseModel):
    project_ids: list[str] = []          # empty = all allowed
    source_ids: list[str] = []
    time_from: TimeExpr | None = None    # RFC 3339 or relative ("now-24h")
    time_to: TimeExpr | None = None
    statuses: list[RunStatus] = []
    is_genai: bool | None = None
    models: list[str] = []
    tools: list[str] = []
    duration_ms: Range[int] | None = None
    tokens: Range[int] | None = None
    cost_usd: Range[Decimal] | None = None
    include_unpriced_in_cost_range: bool = False
    attributes: list[AttrPredicate] = [] # key + op ("eq" | "exists") + value; scope "root" | "resource" (| "any" SHOULD)
    text: str | None = None              # FTS, all terms, case-insensitive [REQ-073]
    session_id: str | None = None
```

URL encoding (Runs page; same names in saved filters). Every parameter is optional and repeatable
where it is plural:

| Param | Example | Maps to |
|---|---|---|
| `project` | `project=billing-bot` (slug) | `project_ids` |
| `source` | `source=planner-svc` (service.name; `ns/name` if namespaced) | `source_ids` |
| `from`, `to` | `from=now-24h`, `to=2026-10-30T12:00:00Z` | time range |
| `status` | `status=error&status=incomplete` | `statuses` |
| `genai` | `genai=1` / `genai=0` | `is_genai` |
| `model`, `tool` | `model=gpt-4o&tool=search_web` | `models`, `tools` |
| `dur`, `tok`, `cost` | `dur=1000..30000`, `tok=..5000`, `cost=0.01..` | ranges (inclusive; open ends allowed) |
| `unpriced` | `unpriced=1` | include unpriced in cost range |
| `attr` | `attr=root:user.tier=gold`, `attr=resource:deployment.environment` (existence) | attribute predicates |
| `q` | `q=timeout%20refund` | text |
| `session` | `session=01JB…` | `session_id` |
| `sort` | `start_desc` (default) \| `duration_desc` \| `tokens_desc` \| `cost_desc` (+ `_asc`) | `sort` |
| `cursor` | opaque | keyset cursor |
| `saved` | `saved=01JB…` | loads a saved filter (explicit params override it) |

Malformed parameters raise `ValidationError(field=...)`. The page shows the error and **never**
an unfiltered list [REQ-101.i].

Cursor = base64url of (sort key value, `trace_id`, `project_id`), signed with the session secret so
it cannot be forged. Cursors are not stable across `sort` changes.

### 6.3 `AdminService` (mutations; `viewer.is_admin` required)

| Method | Effect / returns | REQ |
|---|---|---|
| `create_project(viewer, name, slug)` | `Project` with defaults (retention 30 d, `secrets` preset, content on, default limits) | 170.a |
| `update_project_settings(viewer, project_id, patch: ProjectSettingsPatch)` | retention days, content toggle, rate limits | 173.a, 040 |
| `delete_project(viewer, project_id, confirm_name: str)` | `ValidationError` if `confirm_name` ≠ name. Cascades | 170.d |
| `create_api_key(viewer, project_id, label)` | `CreatedApiKey{key_id, plaintext, snippet}`. Plaintext is never retrievable again | 170.b |
| `list_api_keys(viewer, project_id)` | `list[ApiKeyView]` (prefix, label, created, last used, revoked) | 170 |
| `revoke_api_key(viewer, key_id)` | revokes. Publishes `settings_changed` | 170.c, 006.c |
| `get_pricing(viewer, version: int \| None = None)` | `PricingTable` | 171.a |
| `save_pricing(viewer, entries)` | new `PricingVersion`. Validates non-negative decimals and pattern syntax | 060.c, 171.a |
| `list_unpriced_models(viewer, project_ids)` | `list[UnpricedModel{provider, model, calls, last_seen_ns}]` | 171.b |
| `export_pricing_csv(viewer)` / `import_pricing_csv(viewer, csv_text)` | CSV text / new version (SHOULD) | 171.c |
| `recompute_costs(viewer, project_id, time_range)` | count of LLMCalls updated (SHOULD) | 062 |
| `get_redaction_rules(viewer, project_id)` / `save_redaction_rules(viewer, project_id, rules)` | `RedactionRuleSet`. Regexes compiled and timed at save | 039, 172.a |
| `preview_redaction(viewer, rules, sample: str)` | redacted text/JSON. **Nothing persisted, nothing logged** | 172.b |
| `list_saved_filters` / `save_filter` / `delete_filter` | per Project. Saving is allowed for any viewer with access to the Project (not admin-only) | 102.a |

`RedactionRuleSet`:

```python
class RedactionRuleSet(BaseModel):
    presets: dict[Literal["secrets", "email", "phone", "credit_card"], bool]
    pattern_rules: list[PatternRule]   # name (^[a-z0-9_]{1,32}$), regex, enabled
    field_rules: list[FieldRule]       # key_glob (e.g. "gen_ai.input.messages", "*.email"), action: "drop" | "mask"
```

### 6.4 Subscription helper

`QueryService.subscribe()` wraps `ChangeFeed.subscribe()`. It filters events to the viewer's
allowed Projects, coalesces them (at most one `ChangeNotice` per `min_interval`), and yields
`ChangeNotice{project_ids, run_keys (≤ 200), truncated}`. Pages run it inside a Reflex background
task, one per connected client and page [REQ-140, REQ-145.c].

### 6.5 Demo mode

When the configuration has `demo.enabled = true`, every `AdminService` mutation and
`save_filter` raises `Forbidden("demo mode")`. Reads work normally [REQ-231.b].

### 6.6 Exceptions (`reflex_agentlens.core.errors`)

| Exception | Raised when | Page behavior |
|---|---|---|
| `AgentlensError` | base class | — |
| `ValidationError(field, reason)` | malformed filter/settings input | inline field error |
| `NotFound(entity)` | missing or purged trace/session/project | not-found state [REQ-103.b] |
| `Forbidden(reason)` | not admin, project not allowed, demo mode | "requires admin" / "not authorized" state |
| `Conflict(entity)` | duplicate slug / filter name | inline error |
| `StorageUnavailable` | DB unreachable | "storage unavailable" banner [REQ-330] |

---

## 7. Surface E — Embedding API

### 7.1 Public symbols (`reflex_agentlens`, semver-stable from 0.1.0)

```python
from reflex_agentlens import (
    mount, standalone_app, configure, AgentlensConfig, Viewer, AuthHook, pages,
)

AuthHook = Callable[["rx.State"], Awaitable[Viewer | None]]

def configure(config: AgentlensConfig | None = None) -> None:
    """Register config for this process. No I/O; resources open in the app lifespan."""

def mount(
    app: rx.App,
    *,
    auth: AuthHook,                         # required when embedded          [REQ-191.a]
    config: AgentlensConfig | None = None,  # default: load from env/toml (§8)
    prefix: str = "/agentlens",             # all page routes under it        [REQ-190.b]
    login_route: str | None = None,         # redirect target when auth → None [REQ-191.b]
    ingest: bool = False,                   # mount OTLP/HTTP on the host backend
    ingest_prefix: str = "/agentlens/otlp",
    grpc: bool = False,                     # start the gRPC receiver in the host process
    health: bool = False,                   # mount /healthz, /readyz
    pages: Collection[PageName] = ALL_PAGES,  # subset, e.g. {"runs", "trace"}
    demo: bool = False,                     # read-only demo (site)            [REQ-231]
    nav: bool = True,                       # include agentlens's sub-navigation bar
) -> None: ...

def standalone_app(config: AgentlensConfig | None = None) -> rx.App:   # [REQ-192]
    """mount() on a fresh rx.App with the standalone shell and standalone auth (§7.3)."""
```

`mount()` registers the pages, an app lifespan task (opens storage, migrates if
`database.auto_migrate`, starts purge, finalizer, live-feed and receiver tasks) and, optionally,
the Starlette routes (Reflex 0.10's backend is Starlette; FastAPI is not used). Calling `mount()` twice on one app raises `RuntimeError`. **Importing any
of these symbols performs no I/O** [REQ-190.c].

### 7.2 Pages and the URL contract

Pages in `reflex_agentlens.pages` are component functions. Each one carries its own `on_load`.
They can be mounted individually with the routes below (relative to `prefix`). `pages.ROUTES`
exposes this mapping.

| Page callable | Route (relative) | Query params | REQ |
|---|---|---|---|
| `pages.dashboard` | `/` | `project`, `source`, `from`, `to`, `live=0\|1` | 141 |
| `pages.runs` | `/runs` | §6.2 grammar + `live` | 145 |
| `pages.trace` | `/trace/[trace_id]` | `project` (disambiguation), `tab=waterfall\|graph\|cost\|raw`, `span=<span_id>`, `replay=<span_id>`, `speed=1\|2\|5\|step` | 150–154 |
| `pages.sessions` | `/sessions` | `project`, `source`, `from`, `to`, `sort`, `cursor` | 160.a |
| `pages.session` | `/sessions/[session_id]` | — | 160.b |
| `pages.settings` | `/settings` | `tab=projects\|pricing\|redaction\|retention`, `project` | 170–174 |

Manual mounting, when `mount()` is not wanted:

```python
import reflex as rx
import reflex_agentlens as agentlens

async def my_auth(state: rx.State) -> agentlens.Viewer | None:
    me = await state.get_state(MyAuthState)          # host's own auth state
    if not me.is_logged_in:
        return None
    return agentlens.Viewer(id=me.user_id, display_name=me.name, is_admin=me.is_staff)

app = rx.App()
agentlens.mount(app, auth=my_auth, login_route="/login", prefix="/agentlens")
# or, page by page (still needs configure() + the auth hook):
# agentlens.configure(agentlens.AgentlensConfig(auth_hook=my_auth))
# app.add_page(agentlens.pages.runs, route="/agentlens/runs")
```

**Namespacing** [REQ-190.d]: every agentlens Reflex state class subclasses a single base
`AgentlensState(rx.State)` whose name is prefixed `agentlens_`. Components set no global CSS
outside a `.agentlens` root class. All event handlers call the auth hook (cached per client
token for 30 s) before touching data.

### 7.3 Standalone auth hooks (built in)

| `auth.mode` | Hook behavior | REQ |
|---|---|---|
| `none` | Returns `Viewer(id="local", display_name="local", is_admin=True)` | 224.a/c |
| `password` | Reads `agentlens_session` from `router.headers.cookie`, verifies the signature and expiry, and returns the admin `Viewer`. Otherwise `None` → redirect to `/login` | 224.a, 321 |
| `proxy` | If `router.session.client_ip` ∈ `auth.trusted_proxies`, reads `auth.proxy_user_header`. `is_admin` = (no `proxy_admin_group` set) or the group is in `auth.proxy_groups_header`. Otherwise `None` | 224.a/e |

### 7.4 Content-Security-Policy [REQ-193]

The standalone app sends this baseline. Embedded hosts must allow at least the same directives
on agentlens routes:

```
default-src 'self'; script-src 'self' <build-time 'sha256-…' hashes from _frontend/csp.json>;
connect-src 'self' ws(s)://<host>; img-src 'self' data:; style-src 'self' 'unsafe-inline';
font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self';
frame-ancestors <auth.frame_ancestors, default 'self'>
```

`ws(s)://<host>` is derived per request from the `Host` header (or `server.public_url`).
Reflex 0.10 emits inline scripts without nonces, so they are allow-listed by hash. If spike S1
shows they are not static, `script-src` falls back to `'unsafe-inline'` (Tech Design DD-018).
Emotion injects runtime `<style>` tags, so `style-src` needs `'unsafe-inline'`. `unsafe-eval` and third-party origins are forbidden.

---

## 8. Surface F — Configuration & CLI

### 8.1 Sources and precedence [REQ-202.a]

`defaults` < `agentlens.toml` (path from `--config` or `AGENTLENS_CONFIG`; otherwise
`./agentlens.toml` if it exists) < environment variables < CLI flags.

- TOML: `[section]` + `key`. Environment variable: `AGENTLENS_<SECTION>_<KEY>` (upper snake case).
  Example: `[ingest] max_body_bytes` ↔ `AGENTLENS_INGEST_MAX_BODY_BYTES`.
- Every setting marked 🔒 also accepts `<ENV>_FILE` (path to a file with the value) [REQ-202.c].
  A secret is never printed. `--print-config` masks it as `"***"` [REQ-202.d].
- An unknown key in TOML or an unknown `AGENTLENS_*` variable → startup error naming the key and
  its source [REQ-202.b].

### 8.2 Settings

| TOML `[section] key` | Type | Default | Meaning | REQ |
|---|---|---|---|---|
| `[server] http_host` | str | `127.0.0.1` (`0.0.0.0` in the image) | Bind address of the HTTP port | 224.b |
| `[server] http_port` | int | `8000` (`serve`), `4318` (`collect`) | UI + OTLP/HTTP | 004 |
| `[server] grpc_enabled` | bool | `true` | Start the OTLP/gRPC receiver | 003.c |
| `[server] grpc_host` / `grpc_port` | str / int | = `http_host` / `4317` | gRPC bind | 003 |
| `[server] shutdown_grace` | duration | `20s` | Graceful shutdown bound | 331 |
| `[server] public_url` | URL | unset (derived from `http_host:http_port`) | External URL: banner, CORS allowed origin, CSP `connect-src`, frontend `env.json` rewrite. Reflex's CORS default `*` is never used | 193, 325 |
| `[server] ingest_enabled` | bool | `true` | Mount OTLP receivers in this process (`false` for the `app` service in `compose.postgres.yaml`) | 004 |
| `[server] data_dir` | path | `$XDG_DATA_HOME/reflex-agentlens` (`/data` in the image) | SQLite file, secret files, bootstrap key file | 071.a |
| `[database] url` 🔒 | str | `sqlite:///{data_dir}/agentlens.db` | `sqlite:///…` or `postgresql://…` | 071, 072 |
| `[database] auto_migrate` | bool | `true` | Apply migrations at startup | 074 |
| `[database] pool_size` | int | `10` (Postgres) | Connection pool | — |
| `[ingest] max_body_bytes` | size | `8MiB` | Compressed body limit | 007.b |
| `[ingest] max_decompressed_bytes` | size | `32MiB` | Decompression limit | 007.c |
| `[ingest] queue_memory_bytes` | size | `256MiB` | Pending payload budget | 009.c |
| `[ingest] overload_commit_latency` | duration | `2s` | 503 threshold | 009.b |
| `[ingest] overload_retry_after` | duration | `2s` | `Retry-After` on 503 | 009.b |
| `[ingest] default_request_rate` / `default_request_burst` | int | `50` / `100` | Per-Project defaults | 007.a |
| `[ingest] default_span_rate` / `default_span_burst` | int | `20000` / `40000` | Per-Project defaults | 007.a |
| `[ingest] max_attribute_value_bytes` | size | `256KiB` | Truncation threshold | 041 |
| `[ingest] finalization_window` | duration | `5m` | `in_progress` → `incomplete` | 032.b |
| `[ingest] regex_timeout` | duration | `25ms` | Per-rule per-value budget | 039.d |
| `[ingest] redaction_hook` | `module:function` | unset | Python hook after rules | 039.e |
| `[ingest] store_metrics` | bool | `true` | Persist GenAI metrics | 005 |
| `[auth] mode` | `none` \| `password` \| `proxy` | **unset** (implicit `none` only on a loopback bind) | Standalone UI auth | 224 |
| `[auth] admin_username` | str | `admin` | `password` mode | 224 |
| `[auth] admin_password` 🔒 | str | unset (required in `password` mode) | `password` mode | 224, 321 |
| `[auth] session_secret` 🔒 | str | auto-generated, stored at `{data_dir}/session_secret` (0600) | Cookie and cursor signing | 321 |
| `[auth] session_ttl` | duration | `12h` | Cookie lifetime | 321 |
| `[auth] trusted_proxies` | list[CIDR] | `[]` | `proxy` mode | 224 |
| `[auth] proxy_user_header` | str | `X-Forwarded-User` | `proxy` mode | 224 |
| `[auth] proxy_groups_header` / `proxy_admin_group` | str / str | `X-Forwarded-Groups` / unset | `proxy` admin gating | 224.e |
| `[auth] frame_ancestors` | str | `'self'` | CSP `frame-ancestors` | 193 |
| `[bootstrap] api_key` 🔒 | str (`al_…` format) | unset | Key for the `default` Project | 201.b |
| `[retention] purge_interval` | duration | `1h` | Purge schedule | 092.a |
| `[retention] default_max_age` | duration | `30d` (`0` = forever) | Default for new Projects | 090.a |
| `[retention] storage_size_limit` | size | unset | Global size cap | 091.a |
| `[retention] purge_batch_traces` | int | `500` | Delete batch size | 092.c |
| `[live] enabled` | bool | `true` | Live updates | 140 |
| `[live] min_interval` | duration | `1s` | Coalescing interval per client | 140.b |
| `[live] dashboard_min_interval` | duration | `10s` | Dashboard refresh floor | 141.d |
| `[logging] level` | `DEBUG`…`ERROR` | `INFO` | Log level | 341 |
| `[logging] format` | `json` \| `console` | `json` | `console` is for local development only | 341 |
| `[self_telemetry] enabled` | bool | `false` | Opt-in self-telemetry | 342.c |
| `[self_telemetry] endpoint` | URL | unset | OTLP endpoint. Must not be this instance | 342.a/b |
| `[self_telemetry] protocol` | `http/protobuf` \| `grpc` | `http/protobuf` | — | 342 |
| `[self_telemetry] headers` 🔒 | str (OTel header format) | unset | — | 342 |
| `[pricing] file` | path | bundled | Seed table loaded as version 1 on an empty DB | 060.b |
| `[demo] enabled` | bool | `false` | Read-only demo | 231 |
| `[demo] dataset` | path | bundled sample dataset | Fixtures to load (time-shifted) | 231.c |

`[ingest] default_*` and `[retention] default_max_age` seed **new** Projects. Per-Project values
are then edited in Settings and stored in the DB.

### 8.3 CLI [REQ-200]

```
reflex-agentlens [--config PATH] [COMMAND] [OPTIONS]

  serve            (default) UI + OTLP ingest in one process
      --host, --port, --grpc-port, --no-grpc, --print-config
  collect          OTLP ingest only (requires a PostgreSQL database URL)
      --host, --port, --grpc-port, --no-grpc, --print-config
  demo             serve with demo.enabled=true and the bundled dataset (ingest disabled)
  db upgrade       apply migrations and exit
  db status        print CURRENT | OUTDATED | NEWER and the revision IDs
  rebuild          [--project SLUG] [--since TIME]   recompute derived views   [REQ-042]
  purge            [--dry-run]                       run retention once        [REQ-092.a]
  keys create      --project SLUG [--label TEXT]     print the plaintext key once
  keys list        --project SLUG
  keys revoke      KEY_ID
  version          print package, OTLP proto, and semconv mapping versions
```

| Exit code | Meaning |
|---|---|
| 0 | success / clean shutdown |
| 1 | runtime error |
| 2 | usage or configuration error (invalid/unknown key, `auth.mode` missing on a non-loopback bind, self-telemetry loop) [REQ-202.b, 224.b, 342.b] |
| 3 | port in use (message names the port and the setting) [REQ-200.c] |
| 4 | schema outdated with auto-migrate off, or schema newer than code [REQ-074.b/c] |
| 5 | database unreachable at startup |

`collect` against a SQLite URL exits with code 2 (PRD §12 Q4 constraint; revisited only if the
Technical Design adds a supported mechanism).

**First-run banner** [REQ-201.a], written to the TTY, not through `logging`:

```
agentlens 0.1.0 — UI http://127.0.0.1:8000  ·  OTLP http://127.0.0.1:8000 (HTTP), 127.0.0.1:4317 (gRPC)
Created project "default". API key (shown once):  al_k7m2q9xd_3JcV0pXwQ8rT1nZs6bLk2yHe9GdF4aMu
  export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8000
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20al_k7m2q9xd_…"
```

When stdout is not a TTY, the key line is replaced by
`API key written to /data/bootstrap-api-key (mode 0600)`.

---

## 9. Versioning and compatibility

| Surface | Version policy |
|---|---|
| OTLP ingest | Tracks the OTLP spec. The pinned `opentelemetry-proto` version is in the Technical Design. Newer proto fields are ignored safely (protobuf forward compatibility) |
| Operational HTTP | Stable paths within v0.x |
| Storage protocol | Provisional for external implementers. Changes are listed in the CHANGELOG under "Storage protocol" |
| Query/Admin API, embedding API, page routes, URL grammar | SemVer. During 0.x, breaking changes are allowed only in minor releases (0.1 → 0.2), with CHANGELOG "Breaking" entries and one minor of deprecation warnings where feasible |
| Configuration keys | Renames keep the old key as a deprecated alias for one minor release |
| CLI | Commands and exit codes are stable within v0.x |

---

## 10. Supporting elements without a direct REQ

| Element | Why it exists |
|---|---|
| `X-Agentlens-Key` alternative header | Deployments where `Authorization` is consumed by an auth proxy in front of agentlens |
| `db status` command | Operability companion to REQ-074 |
| `[database] pool_size` | PostgreSQL tuning |
| `distinct_values` / `facets` | Filter-bar value pickers (support REQ-145.b) |
| Signed cursors | Prevents tampering with keyset cursors (support REQ-325) |
| `pages` subset / `nav` options in `mount()` | Hosts that only want some views (e.g. the site demo hides Settings) |

---

## Constitution check

| Article | How this API complies |
|---|---|
| Art. 1 | OTLP is the only ingest surface (§3). No SDK or decorators. Logs are explicitly unimplemented. Unknown attributes travel as raw rows in `NormalizedSpan` |
| Art. 2 | Storage protocol with two backends and a contract suite (§5). No shared rate-limit store: per-process limits documented as a limitation (§3.5). `collect` requires PostgreSQL rather than adding infrastructure |
| Art. 3 | Content-free error messages (§2, §3.3). Plaintext key shown once (§3.4, §6.3). Redaction preview persists and logs nothing. Bootstrap key goes to TTY or a 0600 file. Self-telemetry endpoint loop check |
| Art. 4 | Auth before body read. Size/rate limits with OTLP-correct retryability (§3.3). Hashed keys with constant-time comparison. CSRF guard and HttpOnly cookie (§4). CSP baseline (§7.4). Fail-closed bind check (exit 2) |
| Art. 5 | ns timestamps and `Decimal` money in DTOs (§2). Insert-or-ignore plus a from-scratch Run recompute make ingestion idempotent and order-independent (§5.1) |
| Art. 6 | Layering and the single documented import exception (§1.1). Aggregates are protocol methods that backends implement in SQL. `AgentGraph` is data, with Mermaid rendering in components |
| Art. 7 | `mount()`/pages with no import I/O, host `AuthHook`, namespaced state (§7). `standalone_app()` is `mount()` plus a shell |
| Art. 8 | Every method/endpoint cites REQs. Contract suite defined at the protocol level |
| Art. 9 | `/healthz` and `/readyz` content-free (§4). Self-telemetry settings off by default |
| Art. 10 | Versioning policy (§9). Spec precedes implementation |

**Exceptions requested:** none. **Documented limitations:** per-process rate limits (§3.5);
`serve` not horizontally scalable (PRD §7.4); `collect` requires PostgreSQL (§8.3).

---

## Change history

| Version | Date | Changes |
|---|---|---|
| 1.0-draft.1 | 2026-10-30 | Initial draft |
| 1.0 | 2026-10-30 | Approved by Ernesto Crespo |
| 1.1 | 2026-10-30 | Tech Design amendments A3 (Starlette, not FastAPI), A4 (`server.public_url`, `server.ingest_enabled`), A5 (CSP) |
