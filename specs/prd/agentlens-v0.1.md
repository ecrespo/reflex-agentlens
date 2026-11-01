# reflex-agentlens v0.1

## Product Requirements Document (PRD)

| Field | Value |
|---|---|
| **Author** | Ernesto Crespo (`ecrespo`) |
| **Status** | `APPROVED` |
| **Version** | 1.2 |
| **Date** | 2026-10-30 |
| **Reviewers** | Ernesto Crespo |
| **Last updated** | 2026-11-01 |
| **Governed by** | [`specs/constitution.md`](../constitution.md) v1.0 |

Conventions used in this document:

- Requirement IDs are `REQ-NNN`. Each acceptance criterion has its own ID `REQ-NNN.x`, and that
  ID is what tests cite (Constitution Art. 8). Retired IDs are never reused.
- Every criterion is written in EARS form (THE SYSTEM SHALL / WHEN / WHILE / WHERE / IF … THEN),
  with its pattern noted in brackets: `[U]` ubiquitous, `[E]` event, `[S]` state, `[O]` optional,
  `[X]` unwanted behavior.
- Priority is MoSCoW. v0.1 ships only when **every MUST** is met. A SHOULD may slip to v0.1.x
  with a CHANGELOG note. COULD is best effort.
- Capitalized domain terms (Trace, Span, Run, Session, Source, Project, Derived view) have the
  meaning defined in the Constitution vocabulary.
- "Reference machine" means 4 vCPU (x86-64), 8 GiB RAM, NVMe SSD, Linux. All performance
  criteria are measured there by the benchmark suite (REQ-366).

---

## 1. Executive summary

We are building **reflex-agentlens**, a self-hosted observability tool for AI agents. It is
pure Python, built with Reflex, and released as MIT open source. It ingests standard
**OpenTelemetry (OTLP)** traces and metrics that carry **GenAI semantic-convention** attributes
from any instrumented agent (LangGraph, PydanticAI, OpenAI Agents SDK, Agno, the plain OTel SDK,
or the sibling projects `reflex-agentkit` and `reflex-mcphost`). From those it builds an
agent-level view: Runs, Sessions, LLM calls, tool calls and approvals. Users get live run
timelines, an agent graph view with replay, tool-call inspection, and token and cost analytics.

It targets **agent developers** who want to see what their agent actually did, **platform
engineers in regulated organizations** who cannot ship prompts to a SaaS, and **Reflex app
builders** who want agent observability inside their own app. It runs as one process with an
embedded database (`uvx reflex-agentlens`), as a Docker container, or as a Docker topology with
PostgreSQL. Its pages can also be embedded in a host Reflex app, reusing that app's authentication.

v0.1 succeeds when a developer gets from zero to their first visible Run in **≤ 60 seconds**
(REQ-210), when all seven instrumentation recipes pass their golden tests (REQ-260), and when the
performance targets in §7.1 are met on the reference machine.

## 2. Context and problem

### 2.1 Current situation

- Agent frameworks converged on OpenTelemetry as the telemetry substrate. PydanticAI emits OTel
  natively (through Logfire or the plain SDK). LangChain/LangGraph export OTel through OpenInference
  or LangSmith's OTel bridge. The OpenAI Agents SDK and Agno have OTel instrumentations. The
  OpenTelemetry **GenAI semantic conventions** (`gen_ai.*`) are becoming the shared vocabulary,
  but they are still evolving and implementations diverge.
- The established agent-observability products (LangSmith, Logfire, Langfuse, Arize Phoenix) are
  either SaaS-first, tied to one ecosystem, or need multi-service infrastructure to self-host
  (ClickHouse, Redis, object storage, workers).
- General APMs (Jaeger, Tempo, SigNoz) store the same spans but show a generic waterfall. They do
  not render prompts as conversations, do not compute cost, and do not understand agent graphs,
  tool calls or approvals.

### 2.2 Problem

1. **Privacy blocks adoption.** Prompts and completions contain PHI/PII and business data. Teams
   in health, finance and the public sector cannot send them to a third-party SaaS, so they run
   agents blind.
2. **Self-hosting cost is out of proportion.** For a single team, standing up a multi-service
   stack just to inspect agent runs is more infrastructure than the agent itself.
3. **Generic tracing loses the agent semantics.** A raw span waterfall does not answer "which tool
   failed, what did the model see, how much did this run cost, which path did the graph take".
4. **Observability sits outside the app.** Reflex builders who ship internal agent tools have
   no way to show traces to their users inside the same app and under the same login.

### 2.3 Opportunity

A **standards-only** collector (OTLP in, nothing proprietary) with **zero required
infrastructure**, **agent-aware views**, and **embeddable Reflex pages**. It addresses all four
problems at once and fits next to the author's sibling projects (`reflex-agentkit`,
`reflex-mcphost`, `reflex-silverpoint-react`, `reflex-mermaidjs`) as one coherent toolkit.

## 3. Target users

### Persona 1: Agent developer ("Dana")
- **Description:** Python developer building agents with PydanticAI, LangGraph or the OpenAI Agents SDK, on a laptop and in CI.
- **Main need:** Run the agent, open a page, and see each LLM call, tool call, token count and cost, plus why a run failed or looped.
- **Usage frequency:** Daily, during development.
- **Technical level:** High. Comfortable with environment variables and `uvx`, not interested in operating infrastructure.

### Persona 2: Platform engineer in a regulated organization ("Pablo")
- **Description:** Runs internal platforms at a hospital network or bank. Owns Docker/Kubernetes and PostgreSQL. Answers to compliance.
- **Main need:** Agent observability on-prem with guarantees: redaction before storage, retention limits, hashed credentials, no outbound traffic, non-root containers.
- **Usage frequency:** Weekly (setup, retention and keys); dashboards checked daily.
- **Technical level:** High in operations, medium in Python.

### Persona 3: Reflex app builder ("Rita")
- **Description:** Builds internal tools with Reflex, some of which embed agents (often through `reflex-agentkit`).
- **Main need:** Mount agentlens pages in her own app at `/agentlens/...` behind her app's login, so her users can inspect their agents' runs.
- **Usage frequency:** Integration happens once; her end users then use the pages daily.
- **Technical level:** High in Python/Reflex.

## 4. Goals and success metrics

### 4.1 Project goals

| Goal | Metric | Target | Deadline |
|---|---|---|---|
| Frictionless first experience | Time from `docker compose up` (image already pulled) or `uvx reflex-agentlens` (packages already installed) to the first ingested Run visible in the UI, measured by an automated CI test | ≤ 60 s | v0.1 release |
| Broad instrumentation coverage | Instrumentation recipes with a passing golden integration test | 7 of 7 (REQ-260) | v0.1 release |
| Adoption (aspirational, not a release gate) | GitHub stars / PyPI downloads per month / GHCR pulls | 300 / 2,000 / 500 | v0.1 + 3 months |
| Upstream ecosystem fit | Upstream issues filed: silverpoint chart gaps; shared attribute vocabulary proposal in `reflex-agentkit` and `reflex-mcphost` | All filed, linked from docs | v0.1 release |

### 4.2 User goals

| User goal | Indicator |
|---|---|
| Dana understands a failed run without adding print statements | From the Trace page she can name the failing span, its error and the inputs it received, without leaving the page |
| Dana knows what a run cost | Each Run shows total tokens and cost; unpriced models are flagged explicitly |
| Pablo can show compliance that content was redacted | Redaction preview plus golden tests; canary strings never reach the DB or the logs (REQ-322, REQ-323) |
| Rita embeds pages in under 15 minutes | The embedding guide is about 10 lines of code; the smoke test proves it compiles (REQ-363) |

## 5. Scope

### 5.1 In scope (v0.1)

- [ ] OTLP trace ingestion over HTTP (protobuf and JSON) and gRPC; OTLP metrics ingestion for GenAI metrics
- [ ] Per-Project API keys, rate limits, payload limits, backpressure
- [ ] Normalizer: Spans plus derived Run, Session, LLMCall, ToolCall and Approval views; raw attributes preserved; out-of-order and duplicate safe; redaction before persistence
- [ ] Cost calculation from a versioned, editable pricing table
- [ ] Storage protocol with SQLite (default, FTS5) and PostgreSQL backends, Alembic migrations, retention and purge
- [ ] Query layer: run list with filters, trace detail, span tree, sessions, SQL aggregates
- [ ] UI: Dashboard, Runs, Trace (waterfall, detail drawer, graph view, replay), Sessions, Settings
- [ ] Embedding API (`reflex_agentlens.pages.*`, mount helper, auth hook) and `standalone_app()`
- [ ] CLI, environment variables plus `agentlens.toml` configuration, standalone UI authentication
- [ ] Dockerfile, `compose.yaml`, `compose.postgres.yaml`, GHCR image, `uvx` path
- [ ] Project website `site/` with read-only live demo; MkDocs docs; sample dataset generator
- [ ] Seven instrumentation recipes with golden integration tests
- [ ] CI/CD: lint, types, tests (3.11–3.13, Postgres), Docker build plus healthcheck, PyPI and GHCR on tag
- [ ] README with comparison table, CONTRIBUTING, CHANGELOG, `CLAUDE.md`, `AGENTS.md`

### 5.2 Out of scope (v0.1)

- Agent framework features, prompt management, LLM proxy/gateway (Constitution non-goals 1)
- Agent views for traces without `gen_ai.*` attributes (they are stored and listed only; non-goal 2)
- Proprietary SDKs, decorators, per-framework adapters (non-goal 3)
- Evaluations and judging, annotations, dataset export (v0.2; non-goal 4)
- Multi-tenancy, billing, organizations, per-user RBAC beyond the admin/viewer split (non-goal 5)
- OTLP **logs** ingestion (v0.2; endpoints answer "unimplemented", see REQ-008)
- Retroactive redaction of already-stored data (rules apply to new ingestion only)
- Alerts and webhooks, prompt-version comparison, A/B dashboards (v0.2)
- ClickHouse backend (v0.2, optional)

### 5.3 Future considerations (v0.2 extension points that v0.1 must leave open)

| v0.2 feature | Extension point v0.1 must expose |
|---|---|
| Annotations + dataset export | Stable `run_id`/`span_id` addressing in the query API; storage protocol extendable with new entity types without breaking existing backends |
| PydanticAI evals runner | Query API to fetch a Run's LLM inputs and outputs in normalized form (REQ-104) |
| Alerts via webhooks | Internal post-commit "spans ingested" notification (same channel that live updates use, REQ-140) |
| Prompt-version comparison / A/B | Raw attributes preserved and attribute filters/group-bys in the query layer (REQ-101.f) |
| ClickHouse backend | Storage protocol plus contract test suite that is backend-agnostic (REQ-070) |
| OTLP logs ingestion | Normalizer accepts GenAI events from either span events or a pluggable "log record" input (REQ-034) |

---

## 6. Functional requirements

Index of requirement ranges: Ingestion 001–029 · Normalizer 030–059 · Cost 060–069 ·
Storage 070–089 · Retention 090–099 · Query 100–129 · UI 130–189 · Embedding 190–199 ·
Deployment & CLI 200–229 · Site & demo 230–249 · Recipes 250–269 · Performance 300–319 ·
Security & privacy 320–339 · Self-observability 340–349 · Quality & delivery 360–379 ·
Documentation 380–389.

### 6.1 Ingestion (`reflex_agentlens.ingest`)

#### REQ-001 — OTLP/HTTP trace ingestion, protobuf · MUST
- **REQ-001.a** [E] WHEN a client POSTs an `ExportTraceServiceRequest` encoded as `application/x-protobuf` to the traces endpoint with a valid API key, THE SYSTEM SHALL respond HTTP 200 with an `ExportTraceServiceResponse`, and all spans of the request SHALL be retrievable through the query layer.
- **REQ-001.b** [E] WHEN the request carries `Content-Encoding: gzip`, THE SYSTEM SHALL decompress it before decoding, with the same result as REQ-001.a.
- **REQ-001.c** [X] IF the body is not a decodable `ExportTraceServiceRequest`, THEN THE SYSTEM SHALL respond HTTP 400 with a `google.rpc.Status` body, and SHALL persist nothing from that request.

#### REQ-002 — OTLP/HTTP trace ingestion, JSON · MUST
- **REQ-002.a** [E] WHEN a client POSTs an OTLP/JSON-encoded `ExportTraceServiceRequest` (`application/json`, hex-encoded trace and span IDs per the OTLP JSON mapping) with a valid API key, THE SYSTEM SHALL store spans identical (field by field) to those produced by the protobuf encoding of the same payload.
- **REQ-002.b** [E] WHEN the request is answered, THE SYSTEM SHALL respond with the same encoding the client used (JSON in, JSON out).

#### REQ-003 — OTLP/gRPC trace ingestion · MUST
- **REQ-003.a** [E] WHEN a client calls `opentelemetry.proto.collector.trace.v1.TraceService/Export` with valid API-key metadata, THE SYSTEM SHALL return `OK` with an `ExportTraceServiceResponse`, and the spans SHALL be retrievable through the query layer.
- **REQ-003.b** [U] THE SYSTEM SHALL support gzip-compressed gRPC messages.
- **REQ-003.c** [O] WHERE gRPC ingestion is disabled by configuration, THE SYSTEM SHALL NOT open the gRPC port.

#### REQ-004 — Ingestion in both process topologies · MUST
- **REQ-004.a** [O] WHERE the process runs in `serve` mode (default), THE SYSTEM SHALL expose OTLP/HTTP on the same HTTP port as the UI backend and OTLP/gRPC on the configured gRPC port, inside the same process as the UI.
- **REQ-004.b** [O] WHERE the process runs in `collect` mode, THE SYSTEM SHALL expose OTLP/HTTP and OTLP/gRPC without serving the UI, and SHALL write to the same storage as `serve` instances configured with the same database URL.
- **REQ-004.c** [U] THE SYSTEM SHALL produce identical stored data for the same payload regardless of topology (verified by running the golden suite against both modes).

#### REQ-005 — OTLP metrics ingestion for GenAI metrics · MUST
- **REQ-005.a** [E] WHEN a client exports metrics (HTTP protobuf, HTTP JSON or gRPC `MetricsService/Export`) with a valid API key, THE SYSTEM SHALL respond success and persist data points of the GenAI client metrics (the legacy `gen_ai.client.token.usage` histogram and its replacement `gen_ai.client.inference.usage.*` counters, `gen_ai.client.operation.duration`, the newer duration/time-to-first-chunk histograms, and the server and agent metrics of the pinned semconv revision) with their attributes.
- **REQ-005.b** [E] WHEN an export contains metrics outside the GenAI namespace, THE SYSTEM SHALL respond success, SHALL NOT persist those metrics, and SHALL increment an internal "metrics discarded" counter.

#### REQ-006 — API-key authentication per Project · MUST
- **REQ-006.a** [E] WHEN an ingest request carries a valid, non-revoked API key (through the standard OTLP header mechanism, i.e. settable via `OTEL_EXPORTER_OTLP_HEADERS`), THE SYSTEM SHALL attribute every span and metric in it to that key's Project.
- **REQ-006.b** [X] IF the API key is missing, unknown or revoked, THEN THE SYSTEM SHALL respond HTTP 401 / gRPC `UNAUTHENTICATED` and persist nothing.
- **REQ-006.c** [E] WHEN an API key is revoked in Settings, THE SYSTEM SHALL reject requests with that key within 5 seconds, in every process sharing the database.

#### REQ-007 — Rate limiting and payload limits · MUST
- **REQ-007.a** [X] IF a Project exceeds its configured request or span rate (token bucket, configurable per Project, default 50 requests/s and 20,000 spans/s), THEN THE SYSTEM SHALL respond HTTP 429 with `Retry-After` / gRPC `RESOURCE_EXHAUSTED` with `RetryInfo`, and persist nothing from the rejected request.
- **REQ-007.b** [X] IF a request body exceeds the configured compressed size limit (default 8 MiB), THEN THE SYSTEM SHALL respond HTTP 413 / gRPC `RESOURCE_EXHAUSTED` without `RetryInfo` (non-retryable), without reading the rest of the body.
- **REQ-007.c** [X] IF decompression would exceed the configured decompressed size limit (default 32 MiB), THEN THE SYSTEM SHALL abort decompression and respond as in REQ-007.b (zip-bomb protection).

#### REQ-008 — Unimplemented signals · MUST
- **REQ-008.a** [X] IF a client sends OTLP logs (HTTP `/v1/logs` or gRPC `LogsService/Export`), THEN THE SYSTEM SHALL respond HTTP 404 / gRPC `UNIMPLEMENTED`, so standard exporters stop retrying.

#### REQ-009 — Durable acknowledgement and backpressure · MUST
- **REQ-009.a** [U] THE SYSTEM SHALL return a success response only after all accepted spans of the request are committed to storage. An acknowledged export SHALL survive an immediate `SIGKILL` of the process (verified by a kill test).
- **REQ-009.b** [X] IF the internal write pipeline is saturated (bounded queue full, or storage commit latency above the configured threshold), THEN THE SYSTEM SHALL respond HTTP 503 with `Retry-After` / gRPC `UNAVAILABLE`, and SHALL NOT accept data it cannot commit.
- **REQ-009.c** [U] THE SYSTEM SHALL bound ingest memory: queued, not-yet-committed payloads SHALL NOT exceed the configured memory budget (default 256 MiB).

#### REQ-010 — Partial success · MUST
- **REQ-010.a** [X] IF some spans in an otherwise valid request are invalid (all-zero or wrong-length trace/span ID, end before start), THEN THE SYSTEM SHALL persist the valid spans and respond success with `partial_success.rejected_spans` set to the number of rejected spans and a non-empty `error_message`.

### 6.2 Normalizer

#### REQ-030 — Span persistence with raw attributes · MUST
- **REQ-030.a** [U] THE SYSTEM SHALL persist, for every accepted span: trace ID, span ID, parent span ID, name, kind, start and end time (Unix ns), status code and message, span attributes, resource attributes, instrumentation scope name and version, events (name, time, attributes), links, Project and Source.
- **REQ-030.b** [U] THE SYSTEM SHALL persist every span, resource and event attribute raw (key, typed value) after redaction, including attributes unknown to the pinned semconv revision, so that the raw attribute view of a span equals its OTLP input minus redacted or dropped values.
- **REQ-030.c** [U] THE SYSTEM SHALL record the semconv mapping version used to derive each span's normalized fields.

#### REQ-031 — Source identification · MUST
- **REQ-031.a** [E] WHEN a span arrives, THE SYSTEM SHALL assign it to the Source identified by the resource attributes `service.name` (+ `service.namespace` if present) within the request's Project, creating the Source on first sight.
- **REQ-031.b** [X] IF `service.name` is absent, THEN THE SYSTEM SHALL assign the span to the Source `unknown_service` (OTel's default) of that Project.

#### REQ-032 — Run derivation · MUST
- **REQ-032.a** [U] THE SYSTEM SHALL maintain exactly one Run per (Project, trace ID). Its fields are: start, end, duration, root span name, agent name (from `gen_ai.agent.name` or mapped equivalent), Source(s), models used, total input/output/cached-input tokens, total cost (or unpriced flag), LLM call count, tool call count, error span count, Session ID, `is_genai` flag, and status.
- **REQ-032.b** [U] THE SYSTEM SHALL set Run status to one of: `in_progress` (no root span yet, last span received less than the finalization window ago, default 5 min), `ok` (root span received with status not ERROR), `error` (root span status ERROR), `incomplete` (no root span received within the finalization window after the last span).
- **REQ-032.c** [E] WHEN a root span arrives for a Run in status `incomplete`, THE SYSTEM SHALL recompute the Run and set status `ok` or `error` accordingly.
- **REQ-032.d** [U] THE SYSTEM SHALL set `is_genai = true` on a Run if and only if at least one of its spans carries a `gen_ai.*` attribute (or a mapped equivalent).

#### REQ-033 — LLMCall, ToolCall and Approval derivation · MUST
- **REQ-033.a** [E] WHEN a span's (mapped) `gen_ai.operation.name` is an inference operation (`chat`, `text_completion`, `generate_content`, `embeddings`, plus any added by the pinned revision), THE SYSTEM SHALL derive an LLMCall with provider, request model, response model, input/output/cached-input tokens, finish reasons, request parameters (temperature, max tokens, top-p when present), duration, error, cost, and normalized input/output messages.
- **REQ-033.b** [E] WHEN a span's (mapped) `gen_ai.operation.name` is `execute_tool`, THE SYSTEM SHALL derive a ToolCall with tool name, tool call ID, tool type, arguments, result, duration and status (`ok`/`error` plus error message).
- **REQ-033.c** [E] WHEN a span matches an approval rule of the mapping table (initial entries: `reflex_agentkit.approval.*` and `reflex_mcphost.approval.*` attributes; PydanticAI deferred tool calls `pydantic_ai.tool.deferral.*` as `pending`; COULD: MCP `elicitation/create` spans), THE SYSTEM SHALL derive an Approval with requested-at, decided-at, decision (`approved`/`rejected`/`edited`/`timeout`/`pending`), approver (if present), wait duration and the related tool or node.
- **REQ-033.d** [E] WHEN a span's (mapped) `gen_ai.operation.name` is `invoke_agent` or it matches a graph-node rule of the mapping table, THE SYSTEM SHALL record it as an agent-graph node (node name, agent name, parent node) for the graph view (REQ-153).

#### REQ-034 — Message content in both attribute and event forms · MUST
- **REQ-034.a** [U] THE SYSTEM SHALL normalize message content into one ordered list of `{role, parts}` messages per LLMCall, whether the source sends it as span attributes (`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`), as GenAI span events (deprecated `gen_ai.*.message` / `gen_ai.choice` event form), or as mapped framework attributes (e.g. OpenInference `llm.input_messages.*`).
- **REQ-034.b** [U] THE SYSTEM SHALL implement message extraction behind an input interface that accepts span events today and can accept OTLP log records later (v0.2 extension point) without changing the derived-view schema.

#### REQ-035 — Declarative semconv mapping table · MUST
- **REQ-035.a** [U] THE SYSTEM SHALL derive all normalized fields through one versioned, declarative mapping table (spec attribute ← list of source attribute names, in priority order), covering at least the pinned GenAI revision, its deprecated predecessors still emitted in the wild (e.g. `gen_ai.system`, `gen_ai.usage.prompt_tokens`), OpenInference, and the PydanticAI/Logfire attribute names.
- **REQ-035.b** [E] WHEN a span carries both the spec attribute and a mapped alternative with different values, THE SYSTEM SHALL use the spec attribute.

#### REQ-036 — Session derivation · MUST
- **REQ-036.a** [E] WHEN a span carries a session identifier (`gen_ai.conversation.id`, or a mapped equivalent such as OpenInference `session.id`), THE SYSTEM SHALL associate its Run with that Session (unique per Project), creating the Session on first sight.
- **REQ-036.b** [U] THE SYSTEM SHALL maintain per Session: Run count, first and last activity time, cumulative input/output/cached tokens, and cumulative cost (with an "includes unpriced" flag).
- **REQ-036.c** [X] IF the spans of one trace carry different session identifiers, THEN THE SYSTEM SHALL use the root span's value, or the earliest span's value when there is no root, and keep the others in raw attributes.

#### REQ-037 — Out-of-order and late spans · MUST
- **REQ-037.a** [U] THE SYSTEM SHALL produce identical derived views (Run, Session, LLMCall, ToolCall, Approval) for any arrival order and any batching of the same set of spans (verified by property tests).
- **REQ-037.b** [E] WHEN a child span arrives before its parent, THE SYSTEM SHALL store it immediately and link it to the parent when the parent arrives. The span tree SHALL show it under a "missing parent" placeholder until then.
- **REQ-037.c** [E] WHEN a span arrives for a Run older than the finalization window, THE SYSTEM SHALL recompute that Run and its Session.

#### REQ-038 — Idempotent ingestion · MUST
- **REQ-038.a** [X] IF a span with the same (Project, trace ID, span ID) as a stored span is received, THEN THE SYSTEM SHALL keep the first stored version, store no second row, leave derived views unchanged, and increment an internal "duplicate spans" counter.

#### REQ-039 — Redaction before persistence · MUST
- **REQ-039.a** [U] THE SYSTEM SHALL apply the Project's redaction rules to every string value of span attributes, resource attributes, event attributes, span status messages and exception messages/stack traces **before** any write to storage.
- **REQ-039.b** [U] THE SYSTEM SHALL support two rule kinds: *pattern* rules (regex; each match is replaced by `[REDACTED:<rule-name>]`) and *field* rules (attribute-key glob, action `drop` or `mask`).
- **REQ-039.c** [U] THE SYSTEM SHALL ship built-in pattern presets: `secrets` (common API-key/token formats), `email`, `phone`, `credit_card` (Luhn-validated). New Projects SHALL have `secrets` enabled and the others disabled.
- **REQ-039.d** [X] IF a pattern rule fails to compile or exceeds its per-value time budget, THEN THE SYSTEM SHALL reject the rule at save time (invalid regex) or, at runtime, replace the whole value with `[REDACTED:timeout]` instead of storing it unredacted (fail closed).
- **REQ-039.e** [O] WHERE a Python redaction hook is configured (`module:function`), THE SYSTEM SHALL call it on each span after rule-based redaction and before persistence. IF the hook raises, THEN THE SYSTEM SHALL reject that span (counted in `partial_success`) rather than store it unredacted.
- **REQ-039.f** [E] WHEN redaction rules are changed, THE SYSTEM SHALL apply the new rules to spans ingested after the change, and SHALL show in Settings that existing data is not rewritten.

#### REQ-040 — Content storage toggle per Project · MUST
- **REQ-040.a** [O] WHERE content storage is disabled for a Project, THE SYSTEM SHALL drop all content-bearing attributes and events (message content, system instructions, tool arguments, tool results, as listed in the mapping table) before persistence, keep metadata (models, tokens, timings, tool names, status), and mark affected LLMCalls and ToolCalls `content_dropped = true`.
- **REQ-040.b** [U] THE SYSTEM SHALL enable content storage by default for new Projects (Constitution Art. 3).

#### REQ-041 — Value size limits · SHOULD
- **REQ-041.a** [X] IF a single attribute string value exceeds the configured limit (default 256 KiB), THEN THE SYSTEM SHALL store it truncated at the limit, appended with a `…[truncated N bytes]` marker, and set a `truncated` flag on the span.

#### REQ-042 — Rebuild derived views · MUST
- **REQ-042.a** [E] WHEN an operator runs `reflex-agentlens rebuild [--project P] [--since T]`, THE SYSTEM SHALL recompute all derived views from stored Spans with the current mapping table and pricing, and report counts of Runs changed.
- **REQ-042.b** [U] THE SYSTEM SHALL allow ingestion to continue during a rebuild, and the result SHALL be identical to a rebuild run with ingestion stopped.

### 6.3 Cost calculation

#### REQ-060 — Versioned pricing table · MUST
- **REQ-060.a** [U] THE SYSTEM SHALL keep a pricing table whose entries are (provider, model match pattern, input price, output price, cached-input price; each per 1M tokens, `Decimal`, currency USD), grouped into numbered versions, each with an effective-from timestamp.
- **REQ-060.b** [U] THE SYSTEM SHALL ship a bundled default pricing table as package data, dated in the release notes, and SHALL NOT fetch prices from the network (Constitution Art. 3).
- **REQ-060.c** [E] WHEN an admin saves pricing changes in Settings, THE SYSTEM SHALL create a new version and leave earlier versions unchanged.
- **REQ-060.d** [O] WHERE a cache-write price is set for a model, THE SYSTEM SHALL price cache-creation tokens separately. COULD.

#### REQ-061 — Cost computation · MUST
- **REQ-061.a** [E] WHEN an LLMCall is derived, THE SYSTEM SHALL compute its cost as `input × p_in + output × p_out + cached_input × p_cached` (non-cached input = input − cached_input when the source reports cached as a subset), using the pricing version in effect at the LLMCall's start time, and SHALL store the pricing version ID with the cost.
- **REQ-061.b** [U] THE SYSTEM SHALL match models in this order: response model before request model; exact match before the longest prefix/glob match.
- **REQ-061.c** [X] IF no pricing entry matches, or token counts are missing, THEN THE SYSTEM SHALL store cost NULL with `unpriced = true`, and every aggregate including it SHALL expose an `includes_unpriced` flag or count.

#### REQ-062 — Cost recomputation · SHOULD
- **REQ-062.a** [E] WHEN an admin requests "recompute costs" for a Project and time range, THE SYSTEM SHALL recompute LLMCall, Run and Session costs with the currently effective pricing and record the new pricing version on each.

### 6.4 Storage (`reflex_agentlens.store`)

#### REQ-070 — Storage protocol and contract suite · MUST
- **REQ-070.a** [U] THE SYSTEM SHALL access persistence only through a typed storage protocol (write spans and metrics, read and query, settings CRUD, retention), so that ingest and query code is backend-agnostic.
- **REQ-070.b** [U] THE SYSTEM SHALL provide one backend-agnostic contract test suite that every backend must pass. SQLite and PostgreSQL SHALL pass it in CI.

#### REQ-071 — SQLite backend (default) · MUST
- **REQ-071.a** [O] WHERE no database URL is configured, THE SYSTEM SHALL create and use a SQLite database in the data directory (default `$XDG_DATA_HOME/reflex-agentlens/agentlens.db`, or `/data/agentlens.db` in the container).
- **REQ-071.b** [U] THE SYSTEM SHALL use a database separate from any database the host Reflex app uses for its own state.

#### REQ-072 — PostgreSQL backend · MUST
- **REQ-072.a** [O] WHERE a `postgresql://` database URL is configured, THE SYSTEM SHALL use PostgreSQL (minimum version set in the Technical Design) with identical query results on the contract dataset.
- **REQ-072.b** [O] WHERE several `serve`/`collect` processes share one PostgreSQL database, THE SYSTEM SHALL keep ingestion idempotent and derived views consistent across processes (multi-writer contract test).

#### REQ-073 — Full-text search · MUST
- **REQ-073.a** [U] THE SYSTEM SHALL index for term-based full-text search: span names, message content, tool arguments and results, error/exception messages (all post-redaction). On SQLite this uses FTS5; the PostgreSQL mechanism is decided in the Technical Design.
- **REQ-073.b** [U] THE SYSTEM SHALL return the same set of Runs for the contract dataset's search queries on both backends (ASCII terms, case-insensitive, all-terms match).

#### REQ-074 — Schema migrations · MUST
- **REQ-074.a** [E] WHEN the process starts against a database whose schema is older than the code's, and auto-migrate is enabled (default), THE SYSTEM SHALL apply Alembic migrations before accepting ingest or UI traffic.
- **REQ-074.b** [X] IF auto-migrate is disabled and the schema is outdated, THEN THE SYSTEM SHALL refuse to start and print the command to run (`reflex-agentlens db upgrade`).
- **REQ-074.c** [X] IF the schema is newer than the code, THEN THE SYSTEM SHALL refuse to start with an explicit "downgrade the database or upgrade agentlens" message.
- **REQ-074.d** [O] WHERE several processes start concurrently against PostgreSQL, THE SYSTEM SHALL let exactly one of them apply migrations, while the others wait (lock).

### 6.5 Retention

#### REQ-090 — Retention by age · MUST
- **REQ-090.a** [U] THE SYSTEM SHALL support a per-Project maximum age for trace data (default 30 days; `0` = keep forever).
- **REQ-090.b** [E] WHEN the purge job runs, THE SYSTEM SHALL delete whole Traces (all Spans and derived views) whose Run end time (or last span time, for unfinished Runs) is older than the Project's maximum age, and recompute affected Sessions.

#### REQ-091 — Retention by size · MUST
- **REQ-091.a** [O] WHERE a global storage size limit is configured (default: none), WHEN the purge job runs and stored trace data exceeds the limit, THE SYSTEM SHALL delete the oldest whole Traces across Projects until usage is ≤ 90 % of the limit.
- **REQ-091.b** [U] THE SYSTEM SHALL never purge Projects, API keys, pricing versions, redaction rules or saved filters.

#### REQ-092 — Purge scheduling · MUST
- **REQ-092.a** [U] THE SYSTEM SHALL run the purge job on a configurable interval (default hourly), as a background task inside `serve` or `collect`, and on demand via `reflex-agentlens purge`.
- **REQ-092.b** [O] WHERE several processes share one database, THE SYSTEM SHALL ensure that only one purge job runs at a time.
- **REQ-092.c** [U] THE SYSTEM SHALL delete in bounded batches so that ingestion latency targets (§7.1) hold while a purge runs.

### 6.6 Query and analytics (`reflex_agentlens.query`)

#### REQ-100 — Run list · MUST
- **REQ-100.a** [E] WHEN the run list is queried, THE SYSTEM SHALL return Runs sorted by start time descending (default) or by duration, total tokens or cost, with keyset pagination (default page size 50, max 200).
- **REQ-100.b** [U] THE SYSTEM SHALL return per Run: run ID (trace ID), start, duration, status, root span name, agent name, Source, models, tokens, cost/unpriced, LLM and tool call counts, error count, Session ID, `is_genai`.

#### REQ-101 — Run filters · MUST
THE SYSTEM SHALL support each of the following filters on the run list, combinable with AND semantics:
- **REQ-101.a** [U] Project (one or many) and Source (one or many).
- **REQ-101.b** [U] Time range (absolute or relative) on Run start.
- **REQ-101.c** [U] Status (one or many) and `is_genai`.
- **REQ-101.d** [U] Model (any LLMCall of the Run used the model) and tool name (any ToolCall of the Run used the tool).
- **REQ-101.e** [U] Numeric ranges on duration, total tokens and cost (Runs with unpriced cost excluded from cost ranges unless "include unpriced" is set).
- **REQ-101.f** [U] Attribute filters: equality and existence on any root-span or resource attribute key. SHOULD: on any span of the Run.
- **REQ-101.g** [U] Full-text search (REQ-073) over the Run's spans.
- **REQ-101.h** [U] Session ID.
- **REQ-101.i** [X] IF a filter value is malformed (unparseable number or time, unknown status), THEN THE SYSTEM SHALL return a validation error naming the field, and never an unfiltered result.

#### REQ-102 — Saved filters · MUST
- **REQ-102.a** [E] WHEN a user saves the current filter set with a name, THE SYSTEM SHALL persist it per Project (shared by all viewers of that Project), and it SHALL be reloadable from the Runs page and addressable via URL query parameters.
- **REQ-102.b** [U] THE SYSTEM SHALL encode the active filter set in the Runs page URL, so that any filtered view is shareable as a link.

#### REQ-103 — Trace detail and span tree · MUST
- **REQ-103.a** [E] WHEN a trace is requested by ID, THE SYSTEM SHALL return the Run, its Spans as a tree (parent/child, siblings ordered by start time), each span's derived LLMCall/ToolCall/Approval if any, and token and cost totals per span subtree.
- **REQ-103.b** [X] IF the trace ID does not exist (or was purged), THEN THE SYSTEM SHALL return "not found", and the Trace page SHALL show a not-found state with a link back to Runs.

#### REQ-104 — Normalized LLM I/O access · MUST
- **REQ-104.a** [E] WHEN the LLM calls of a Run are requested, THE SYSTEM SHALL return each LLMCall's normalized messages (REQ-034), parameters, tokens and cost in a stable, documented shape (the v0.2 evals/dataset extension point).

#### REQ-105 — Session queries · MUST
- **REQ-105.a** [E] WHEN Sessions are listed, THE SYSTEM SHALL return Sessions filterable by Project, Source and time range, sortable by last activity, Run count, tokens or cost, with keyset pagination.
- **REQ-105.b** [E] WHEN one Session is requested, THE SYSTEM SHALL return its Runs in chronological order with per-Run and cumulative tokens and cost.

#### REQ-106 — Dashboard aggregates computed in SQL · MUST
For a Project/Source filter and a time range, THE SYSTEM SHALL compute, in SQL, with an automatic time bucket (minute/hour/day by range):
- **REQ-106.a** [U] Runs over time, split by status.
- **REQ-106.b** [U] Tokens (input/output/cached) and cost by model, over time and in total.
- **REQ-106.c** [U] Latency p50 and p95 (SHOULD: p99) per operation name and per agent-graph node.
- **REQ-106.d** [U] Tool call count, success count and error rate per tool, and the top N tools by call count.
- **REQ-106.e** [U] Cost and tokens by Source.
- **REQ-106.f** [U] Approval outcomes (count per decision) and median wait time.
- **REQ-106.g** [U] KPI totals: Runs, error rate, total tokens, total cost with unpriced count, p95 Run duration, active Sessions.
- **REQ-106.h** [U] THE SYSTEM SHALL compute every aggregate above with SQL aggregate/window functions, so that no aggregate iterates rows in Python (verified by a test that asserts a bounded number of rows fetched per aggregate).

#### REQ-107 — Metrics aggregates · SHOULD
- **REQ-107.a** [E] WHEN GenAI metric data points exist for the selected range, THE SYSTEM SHALL return token usage by model over time, as reported by `gen_ai.client.inference.usage.*` (preferred) or the legacy `gen_ai.client.token.usage`.

### 6.7 UI (`reflex_agentlens.pages`, `reflex_agentlens.components`)

General UI requirements:

#### REQ-130 — Shared page chrome · MUST
- **REQ-130.a** [U] THE SYSTEM SHALL provide on every page a Project selector, a Source selector and a time-range selector (last 1 h, 24 h, 7 d, 30 d, custom), persisted in the URL.
- **REQ-130.b** [U] THE SYSTEM SHALL use theme tokens aligned with the silverpoint palette for all colors (status, span kinds, charts), defined in one place.
- **REQ-130.c** [U] THE SYSTEM SHALL render all charts with `reflex-silverpoint-react` and all graphs with `reflex-mermaidjs` (Constitution Art. 7).
- **REQ-130.d** [X] IF a query fails, THEN THE SYSTEM SHALL show an inline error state on the affected component, with no stack trace or content in the message, while the rest of the page remains usable.
- **REQ-130.e** [U] THE SYSTEM SHALL show an empty state with the quickstart snippet (endpoint URL, header name, environment variables) on Dashboard and Runs when the selected Project has no data.

#### REQ-131 — Generic (non-GenAI) traces · MUST
- **REQ-131.a** [E] WHEN a Run has `is_genai = false`, THE SYSTEM SHALL list it with a "generic" badge, and its Trace page SHALL show the waterfall and raw attributes but no graph view, LLM, tool or cost panels (non-goal 2).

**Dashboard page**

#### REQ-140 — Live updates channel · MUST
- **REQ-140.a** [U] THE SYSTEM SHALL notify open UI sessions of newly committed spans for their selected Project. New data SHALL appear on live views within 3 s p95 of the ingest acknowledgement, in every topology (in-process and separate `collect`). The mechanism is decided in the Technical Design.
- **REQ-140.b** [U] THE SYSTEM SHALL coalesce notifications so that a UI session receives at most one refresh per second, regardless of ingest rate.

#### REQ-141 — Dashboard · MUST
- **REQ-141.a** [U] THE SYSTEM SHALL show KPI cards for the aggregates of REQ-106.g.
- **REQ-141.b** [U] THE SYSTEM SHALL show silverpoint charts for: Runs per bucket by status (REQ-106.a); tokens and cost by model (REQ-106.b); p50/p95 latency by operation (REQ-106.c); tool success vs. error and top tools (REQ-106.d); cost by Source (REQ-106.e); approval outcomes (REQ-106.f).
- **REQ-141.c** [E] WHEN a user clicks a chart element that maps to a filter (model, tool, Source, status, time bucket), THE SYSTEM SHALL navigate to Runs with that filter applied. SHOULD.
- **REQ-141.d** [S] WHILE live mode is on (default), THE SYSTEM SHALL refresh dashboard aggregates at most every 10 s when new data arrives.
- **REQ-141.e** [O] WHERE GenAI metric data exists, THE SYSTEM SHALL show a "token usage (metrics)" chart (REQ-107). COULD.

**Runs page**

#### REQ-145 — Runs table · MUST
- **REQ-145.a** [U] THE SYSTEM SHALL show the run list (REQ-100) as a table with columns: start, status, agent / root name, Source, duration, models, tokens, cost (or "unpriced"), Session. Clicking a row opens the Trace page.
- **REQ-145.b** [U] THE SYSTEM SHALL expose every REQ-101 filter in a filter bar, plus save/load of saved filters (REQ-102).
- **REQ-145.c** [S] WHILE live mode is on, the first page is shown, and the sort is start time descending, THE SYSTEM SHALL insert new Runs at the top and update `in_progress` rows within the REQ-140 latency.
- **REQ-145.d** [S] WHILE the user is on any other page or sort order, THE SYSTEM SHALL NOT shift rows, and SHALL instead show an "N new runs" indicator that returns to the live first page when clicked.

**Trace page**

#### REQ-150 — Waterfall timeline · MUST
- **REQ-150.a** [U] THE SYSTEM SHALL render the span tree as a collapsible waterfall on a shared time axis, each bar labeled GenAI-aware (e.g. `chat gpt-4o · 1.2k tok`, `execute_tool search_web`, `invoke_agent planner`) and colored by kind (LLM, tool, agent, approval, generic, error).
- **REQ-150.b** [S] WHILE the Run is `in_progress`, THE SYSTEM SHALL append arriving spans to the waterfall within the REQ-140 latency.
- **REQ-150.c** [U] THE SYSTEM SHALL render traces of up to 5,000 spans, collapsing subtrees beyond depth 3 by default, without the browser tab becoming unresponsive for more than 1 s (measured in the UI smoke benchmark). SHOULD.

#### REQ-151 — Span detail drawer · MUST
- **REQ-151.a** [E] WHEN a span is selected, THE SYSTEM SHALL open a detail drawer showing: name, kind, timing, status, Source.
- **REQ-151.b** [E] WHEN the selected span is an LLMCall, THE SYSTEM SHALL render its messages as role bubbles (system, user, assistant, tool) with tool-call parts shown as JSON, plus model, parameters, tokens and cost.
- **REQ-151.c** [E] WHEN the selected span is a ToolCall, THE SYSTEM SHALL render its arguments and result as pretty-printed, collapsible JSON (or plain text if not JSON).
- **REQ-151.d** [E] WHEN the selected span has status ERROR or exception events, THE SYSTEM SHALL show the error message, exception type and stack trace.
- **REQ-151.e** [U] THE SYSTEM SHALL provide a "raw" tab with all span, resource and event attributes exactly as stored.
- **REQ-151.f** [U] THE SYSTEM SHALL render all content as escaped text, or as sanitized markdown with no raw HTML, scripts or remote images (see REQ-327).
- **REQ-151.g** [E] WHEN content was dropped (REQ-040) or redacted, THE SYSTEM SHALL display an explicit "content not stored" or `[REDACTED:<rule>]` marker, never an empty bubble.

#### REQ-152 — Token and cost breakdown · MUST
- **REQ-152.a** [U] THE SYSTEM SHALL show per Trace: totals and a per-LLMCall table (model, input/output/cached tokens, cost, pricing version), with unpriced calls flagged.

#### REQ-153 — Graph view · MUST
- **REQ-153.a** [E] WHEN the graph tab is opened for a GenAI Run with agent-graph nodes (REQ-033.d), THE SYSTEM SHALL render, with `reflex-mermaidjs`, the graph of nodes **executed** in the Run, with edges following parent/sequence order. Nodes and edges on the taken path SHALL be highlighted.
- **REQ-153.b** [U] THE SYSTEM SHALL mark retried nodes with an execution count, interrupted/approval nodes with a distinct style, and error nodes with the error style.
- **REQ-153.c** [E] WHEN a graph node is clicked, THE SYSTEM SHALL select the corresponding span in the waterfall and open its detail drawer.
- **REQ-153.d** [X] IF the graph exceeds 150 nodes, THEN THE SYSTEM SHALL collapse repeated node executions into one node with a count, and offer the full view on demand.
- **REQ-153.e** [X] IF a Run has no agent-graph nodes, THEN THE SYSTEM SHALL fall back to a tool/LLM-call sequence diagram and label it as such.

#### REQ-154 — Replay mode · MUST
- **REQ-154.a** [E] WHEN replay is started, THE SYSTEM SHALL step through the Run's spans in start-time order, highlighting the current span in the waterfall and in the graph view, and showing its detail drawer.
- **REQ-154.b** [U] THE SYSTEM SHALL offer play, pause, step forward, step back, jump to start/end, and speeds 1× (real relative timing, idle gaps capped at 2 s), 2×, 5× and "step only".
- **REQ-154.c** [U] THE SYSTEM SHALL keep the replay position in the URL (`?replay=<span_id>`) so a position is shareable.

**Sessions page**

#### REQ-160 — Sessions list and detail · MUST
- **REQ-160.a** [U] THE SYSTEM SHALL show the Session list (REQ-105.a) with Run count, first/last activity, cumulative tokens and cost. The UI label is "Sessions (threads)".
- **REQ-160.b** [E] WHEN a Session is opened, THE SYSTEM SHALL show its Runs chronologically with per-Run and cumulative tokens and cost, each Run linking to its Trace page.
- **REQ-160.c** [U] THE SYSTEM SHALL show a cumulative cost/tokens silverpoint chart for the Session. SHOULD.

**Settings page**

#### REQ-170 — Projects and API keys · MUST
- **REQ-170.a** [E] WHEN an admin creates a Project, THE SYSTEM SHALL persist it with default settings (retention 30 days, `secrets` redaction preset on, content storage on, default rate limits).
- **REQ-170.b** [E] WHEN an admin creates an API key, THE SYSTEM SHALL display the plaintext key exactly once with a copy button and a ready-to-paste `OTEL_EXPORTER_OTLP_*` snippet, and store only its hash plus a non-secret prefix for identification.
- **REQ-170.c** [E] WHEN an admin revokes an API key, THE SYSTEM SHALL record the revocation time, and the key SHALL be rejected per REQ-006.c.
- **REQ-170.d** [X] IF an admin deletes a Project, THEN THE SYSTEM SHALL require typing the Project name to confirm, then delete its keys and all its trace data.

#### REQ-171 — Pricing editor · MUST
- **REQ-171.a** [U] THE SYSTEM SHALL let admins view the current pricing version, add/edit/remove entries, and save them as a new version (REQ-060.c), with input validated as non-negative decimals.
- **REQ-171.b** [U] THE SYSTEM SHALL list models seen in LLMCalls that have no pricing entry ("unpriced models") with a one-click "add price" action.
- **REQ-171.c** [U] THE SYSTEM SHALL import and export the pricing table as CSV. SHOULD.

#### REQ-172 — Redaction rules editor · MUST
- **REQ-172.a** [U] THE SYSTEM SHALL let admins enable/disable presets and add/edit/remove pattern and field rules per Project, with regex validation at save time.
- **REQ-172.b** [E] WHEN an admin pastes sample text or a sample span JSON into the preview box, THE SYSTEM SHALL show the redacted output under the current unsaved rules without persisting the sample. SHOULD.

#### REQ-173 — Retention and content settings · MUST
- **REQ-173.a** [U] THE SYSTEM SHALL let admins set per-Project maximum age and the content storage toggle, and SHALL show the global size limit and current storage usage.
- **REQ-173.b** [U] THE SYSTEM SHALL show the time of the last purge run and how many Traces it deleted.

#### REQ-174 — Admin gating · MUST
- **REQ-174.a** [X] IF a viewer without admin rights opens Settings or calls a settings-mutating event, THEN THE SYSTEM SHALL deny the action and show a "requires admin" state.

### 6.8 Embedding API

#### REQ-190 — Embeddable pages · MUST
- **REQ-190.a** [U] THE SYSTEM SHALL export page callables `reflex_agentlens.pages.dashboard`, `.runs`, `.trace`, `.sessions`, `.session`, `.settings` that a host app can mount with `app.add_page(page, route=...)`.
- **REQ-190.b** [U] THE SYSTEM SHALL provide a one-call mount helper that registers all pages under a configurable route prefix (default `/agentlens`), and optionally mounts the OTLP/HTTP endpoints on the host's backend.
- **REQ-190.c** [U] Importing `reflex_agentlens` or any `pages` module SHALL perform no I/O: no database connection, thread start or socket bind (verified by a test that imports with networking and filesystem writes blocked).
- **REQ-190.d** [U] THE SYSTEM SHALL namespace all its Reflex state classes and routes so that mounting it creates no name collision with host state or routes (smoke test with a host app that defines same-named states).

#### REQ-191 — Host authentication hook · MUST
- **REQ-191.a** [O] WHERE agentlens is embedded, THE SYSTEM SHALL call a host-provided auth hook on each page load and before each event that reads or changes data, and expect a viewer identity (ID, display name, `is_admin`) or "unauthenticated". For events (not page loads) the result MAY be reused for at most 30 s, and only for the same client token.
- **REQ-191.b** [X] IF the hook returns "unauthenticated", THEN THE SYSTEM SHALL render no agentlens data and SHALL redirect to the host-configured login route (or show a "not authorized" state if none is configured).
- **REQ-191.c** [O] WHERE the hook also returns a set of allowed Project IDs, THE SYSTEM SHALL restrict every query and selector to those Projects.

#### REQ-192 — Standalone app · MUST
- **REQ-192.a** [U] THE SYSTEM SHALL provide `reflex_agentlens.standalone_app()`, returning an `rx.App` built only from the exported pages plus a navigation shell and the standalone auth (REQ-224). No page SHALL exist only in standalone mode (Constitution Art. 7).

#### REQ-193 — Content-Security-Policy for embedded pages · MUST
- **REQ-193.a** [U] THE SYSTEM SHALL document the CSP directives the pages need, and the standalone app SHALL send a CSP header containing no `unsafe-eval` and no third-party origins (verified by an HTTP header test). The handling of `unsafe-inline` for styles is decided in the Technical Design based on Reflex's needs.

### 6.9 Deployment, CLI and configuration

#### REQ-200 — CLI · MUST
- **REQ-200.a** [U] THE SYSTEM SHALL install a `reflex-agentlens` CLI with commands: `serve` (default when no command is given: UI + ingest), `collect` (ingest only), `db upgrade`, `rebuild`, `purge`, `keys create|list|revoke`, `demo` (serve with the bundled sample dataset, read-only), `version`.
- **REQ-200.b** [U] `uvx reflex-agentlens` SHALL start `serve` with no prior setup and **without Node.js/bun installed on the machine** (the frontend ships pre-built in the wheel).
- **REQ-200.c** [X] IF the configured ports are in use, THEN THE SYSTEM SHALL exit non-zero with a message naming the port and the setting to change it.

#### REQ-201 — First-run bootstrap · MUST
- **REQ-201.a** [E] WHEN `serve` starts against an empty database, THE SYSTEM SHALL create a Project `default` with an API key. The plaintext key SHALL be written directly to the terminal banner (not via the logging subsystem) if stdout is a TTY. Otherwise it SHALL be written to a `0600` file in the data directory, and the banner SHALL print that file's path.
- **REQ-201.b** [O] WHERE `AGENTLENS_BOOTSTRAP_API_KEY` is set, THE SYSTEM SHALL use that value (hashed) as the `default` Project's key instead of generating one, and never print it.

#### REQ-202 — Configuration surface · MUST
- **REQ-202.a** [U] THE SYSTEM SHALL read configuration from defaults, then an optional `agentlens.toml` (path from `--config` or `AGENTLENS_CONFIG`), then `AGENTLENS_*` environment variables, with later sources overriding earlier ones.
- **REQ-202.b** [X] IF any configuration value is invalid or unknown, THEN THE SYSTEM SHALL exit non-zero at startup with a message naming the key and its source, before opening any port.
- **REQ-202.c** [U] THE SYSTEM SHALL accept every secret setting (database URL password, admin password, bootstrap key) either as a value or as a `*_FILE` path (Docker secrets convention).
- **REQ-202.d** [U] `reflex-agentlens serve --print-config` SHALL print the effective configuration with secrets masked.

#### REQ-203 — Container image · MUST
- **REQ-203.a** [U] THE SYSTEM SHALL provide a multi-stage `Dockerfile` that produces an image running as a non-root UID, exposing the HTTP port and the OTLP/gRPC port, with a `HEALTHCHECK` against `/healthz`, and with `/data` as the volume for SQLite.
- **REQ-203.b** [U] The image SHALL contain the pre-built frontend and no build toolchain (no Node.js, compilers or package caches). Target size ≤ 500 MB uncompressed (SHOULD).
- **REQ-203.c** [U] The same image SHALL run `serve` or `collect` depending on its command.

#### REQ-204 — `compose.yaml` (try-it) · MUST
- **REQ-204.a** [E] WHEN a user runs `docker compose up` with the shipped `compose.yaml`, THE SYSTEM SHALL start one container with SQLite on a named volume, publish the UI/OTLP-HTTP port bound to `127.0.0.1` only, and become healthy.

#### REQ-205 — `compose.postgres.yaml` (production) · MUST
- **REQ-205.a** [E] WHEN a user runs `docker compose -f compose.postgres.yaml up` with the documented `.env`, THE SYSTEM SHALL start: `app` (`serve`), `collector` (`collect`), PostgreSQL with a persistent volume, and Caddy terminating TLS in front of the UI and OTLP/HTTP, with all services healthy.
- **REQ-205.b** [U] In that topology, only Caddy SHALL publish ports to the host. PostgreSQL SHALL be reachable only on the internal network.
- **REQ-205.c** [U] The `collector` service SHALL be horizontally scalable (`--scale collector=N`) without data loss or duplicates (multi-writer test, REQ-072.b).

#### REQ-206 — Publishing · MUST
- **REQ-206.a** [E] WHEN a `vX.Y.Z` tag is pushed on `main`, CI SHALL publish the wheel and sdist to PyPI via trusted publishing, and push `ghcr.io/ecrespo/reflex-agentlens:X.Y.Z`, `:X.Y` and `:latest` images for `linux/amd64` and `linux/arm64`.

#### REQ-210 — 60-second time-to-first-Run · MUST
- **REQ-210.a** [E] WHEN `docker compose up` is run with the image already pulled, and the recorded PydanticAI fixture is then sent with the bootstrap key, THE SYSTEM SHALL show the Run in the run list query within 60 s of the `up` command (automated CI test).
- **REQ-210.b** [E] WHEN `uvx reflex-agentlens` is run with packages already cached, THE SYSTEM SHALL serve the UI and accept ingest within 30 s on the reference machine.

#### REQ-224 — Standalone UI authentication · MUST
- **REQ-224.a** [U] THE SYSTEM SHALL support UI auth modes `none`, `password` (single admin credential from config, signed session cookie, login page) and `proxy` (trust a configured user header only from configured trusted proxy CIDRs).
- **REQ-224.b** [X] IF the HTTP server binds to a non-loopback address and no auth mode is explicitly configured, THEN THE SYSTEM SHALL refuse to start with a message explaining the options.
- **REQ-224.c** [O] WHERE the mode is `none` on a non-loopback bind (explicitly configured, as in `compose.yaml` behind a `127.0.0.1` port mapping), THE SYSTEM SHALL log a startup warning.
- **REQ-224.d** [O] WHERE the mode is `password`, THE SYSTEM SHALL throttle failed logins (≥ 1 s delay after 5 failures per IP per minute).
- **REQ-224.e** [U] In `password` and `proxy` modes, every authenticated user is admin in v0.1. In `proxy` mode, an optional admin-group header can restrict admin rights.

### 6.10 Project website and demo (`site/`)

#### REQ-230 — Landing page · MUST
- **REQ-230.a** [U] The `site/` Reflex app SHALL provide a landing page with: tagline and pitch, a 60-second quickstart (`uvx` and Docker tabs), feature grid, screenshots/GIFs, a "Why not SaaS" section, the sibling projects section, and links to docs, GitHub, PyPI and the changelog.
- **REQ-230.b** [U] The site SHALL use silverpoint charts for every chart it shows (e.g. a footprint comparison).

#### REQ-231 — Live demo · MUST
- **REQ-231.a** [U] The site SHALL embed the agentlens pages (via the embedding API, REQ-190) in demo mode, fed by the bundled sample dataset.
- **REQ-231.b** [S] WHILE in demo mode, THE SYSTEM SHALL disable ingest endpoints, hide Settings mutations (read-only Settings view), and reject every mutating event.
- **REQ-231.c** [U] Demo data timestamps SHALL be shifted at load so the dataset always looks recent (last 7 days).

#### REQ-232 — Docs and changelog · MUST
- **REQ-232.a** [U] Docs SHALL be built with MkDocs Material from `docs/` (CI fails on broken internal links), and linked from the site.
- **REQ-232.b** [U] The site SHALL render `CHANGELOG.md` as a changelog page.

#### REQ-233 — Sample dataset generator · MUST
- **REQ-233.a** [E] WHEN the generator script runs, THE SYSTEM SHALL execute the bundled PydanticAI and LangGraph example agents against a deterministic fake LLM, capture their OTel output, and write OTLP/JSON fixture files.
- **REQ-233.b** [U] The generator SHALL be deterministic for a given seed (byte-identical output, modulo timestamps normalized to a fixed epoch).
- **REQ-233.c** [U] The dataset SHALL include at least: successful multi-tool runs, a tool error with recovery, a failed run, a retried graph node, an approval (approved and rejected), multi-run Sessions, an unpriced model, and one generic non-GenAI trace.

### 6.11 Instrumentation recipes (`docs/instrument/`)

#### REQ-250 — Recipe set · MUST
- **REQ-250.a** [U] THE SYSTEM SHALL document recipes for: PydanticAI (Logfire / OTel exporter), LangGraph/LangChain (OpenInference as primary; LangSmith OTel export as secondary), OpenAI Agents SDK (OpenInference instrumentation; the OTel contrib `openai-agents-v2` package listed as an alternative), Agno (OpenInference via `agno.tracing`), `reflex-agentkit`, `reflex-mcphost`, and the plain OTel SDK. Each recipe SHALL contain a copy-paste snippet, the required environment variables, and the known attribute divergences.

#### REQ-251 — Shared attribute vocabulary for sibling projects · MUST
- **REQ-251.a** [U] THE SYSTEM SHALL document the `reflex_agentkit.*` and `reflex_mcphost.*` span attribute vocabulary that agentlens understands (approvals, AG-UI events, MCP server/tool identity), and an issue proposing it SHALL be filed upstream in both repositories.

#### REQ-260 — Recipe golden integration tests · MUST
- **REQ-260.a** [U] For each of the seven recipes, a recorded OTLP fixture SHALL be stored in the repo, and an integration test SHALL ingest it through the real HTTP endpoint and assert the derived Run fields: status, agent name, models, LLM call count, tool call count, token totals, cost/unpriced, Session ID.
- **REQ-260.b** [U] The tests SHALL run offline (no network, no API keys), and the fixtures SHALL record the framework and instrumentation versions that produced them.

---

## 7. Non-functional requirements

### 7.1 Performance targets

All measured on the reference machine by the benchmark suite (REQ-366), with results published
as CI artifacts. "100k-span dataset" means a synthetic dataset generated from the sample-dataset
distributions, about 20 spans per Run.

#### REQ-300 — Ingest throughput, SQLite single process · MUST
- **REQ-300.a** [S] WHILE running `serve` with SQLite and the default redaction preset, THE SYSTEM SHALL sustain ≥ **2,000 spans/s** for 10 minutes (OTLP/HTTP protobuf, 512-span batches, 4 concurrent clients) with zero 5xx responses and zero data loss.

#### REQ-301 — Ingest throughput, PostgreSQL collector · MUST
- **REQ-301.a** [S] WHILE running one `collect` process against PostgreSQL on the same host, THE SYSTEM SHALL sustain ≥ **5,000 spans/s** for 10 minutes under the REQ-300 load shape.
- **REQ-301.b** [S] WHILE running two `collect` processes, THE SYSTEM SHALL sustain ≥ 1.6× the single-collector throughput. SHOULD.

#### REQ-302 — Ingest latency · MUST
- **REQ-302.a** [U] THE SYSTEM SHALL answer ingest requests (512 spans) in p95 ≤ **250 ms** at 50 % of the REQ-300/301 throughput.

#### REQ-303 — Runs page latency · MUST
- **REQ-303.a** [U] THE SYSTEM SHALL return the first page of the run list, with any single REQ-101 filter or full-text search applied, in server-side p95 < **300 ms** on the 100k-span dataset (SQLite and PostgreSQL).
- **REQ-303.b** [U] Same query on a 1M-span dataset on PostgreSQL: p95 < 300 ms. SHOULD.

#### REQ-304 — Trace page latency · MUST
- **REQ-304.a** [U] THE SYSTEM SHALL return the trace detail (REQ-103) for a 1,000-span trace in server-side p95 < **500 ms** (both backends).

#### REQ-305 — Dashboard latency · MUST
- **REQ-305.a** [U] THE SYSTEM SHALL compute the full dashboard aggregate set (REQ-106) for a 30-day range in p95 < **1.5 s** on a 1M-span SQLite database and < **1 s** on a 1M-span PostgreSQL database. *This target decides the DuckDB question (§12, Q2).*

#### REQ-306 — Live update latency · MUST
- **REQ-306.a** [U] Ack-to-visible latency on live views SHALL be p95 ≤ 3 s in both topologies (REQ-140.a).

#### REQ-307 — Resource footprint · SHOULD
- **REQ-307.a** [U] `serve` with SQLite SHALL use ≤ 600 MiB RSS at idle with the 100k-span dataset, and ≤ 1 GiB RSS during the REQ-300 load test.

### 7.2 Security and privacy

#### REQ-320 — API keys hashed at rest · MUST
- **REQ-320.a** [U] THE SYSTEM SHALL store API keys only as salted, slow or keyed hashes (algorithm chosen in the Technical Design). No table, log or backup SHALL contain a plaintext key (verified by scanning the DB and logs after the test suite for every generated key).
- **REQ-320.b** [U] THE SYSTEM SHALL generate keys with ≥ 128 bits of entropy from a CSPRNG, with a recognizable prefix (e.g. `al_`) for secret scanners.

#### REQ-321 — Admin credential handling · MUST
- **REQ-321.a** [U] THE SYSTEM SHALL keep the `password` auth mode credential only in configuration (or hashed if persisted). Session cookies SHALL be signed, `HttpOnly`, `SameSite=Lax`, and `Secure` when served over HTTPS.

#### REQ-322 — Redaction before persistence, verified · MUST
- **REQ-322.a** [U] THE SYSTEM SHALL pass a canary test: after ingesting spans containing strings that match each enabled rule, a byte-level scan of the database files (SQLite file + WAL, or a PostgreSQL dump) finds zero canary occurrences.

#### REQ-323 — No content in logs or self-telemetry · MUST
- **REQ-323.a** [U] THE SYSTEM SHALL never write span attribute values, message content, tool arguments/results or API keys to logs, error responses or self-telemetry. Logs may contain IDs, counts, sizes, durations, attribute **keys** and error classes.
- **REQ-323.b** [U] Verified by a canary test: running the full ingest/query test suite with `DEBUG` logging and self-telemetry captured yields zero canary occurrences in the captured output.

#### REQ-324 — No unconfigured outbound traffic · MUST
- **REQ-324.a** [U] THE SYSTEM SHALL open no outbound network connection except to: the configured database, the configured self-telemetry endpoint (if enabled), and connections the host app makes. Verified by a test that runs `serve` + the UI smoke test with outbound sockets intercepted.
- **REQ-324.b** [U] The standalone and embedded frontend SHALL load no asset from a third-party origin (fonts, scripts, styles, images included).

#### REQ-325 — Input validation at the edge · MUST
- **REQ-325.a** [U] THE SYSTEM SHALL validate all inbound OTLP payloads against the protobuf/JSON schema, and all UI/query inputs (filters, settings forms) with typed models, before use.
- **REQ-325.b** [U] THE SYSTEM SHALL build every SQL statement with bound parameters, including attribute-filter keys and full-text queries (FTS query syntax escaped). Verified by injection test cases in the query test suite.

#### REQ-326 — Container hardening · MUST
- **REQ-326.a** [U] The image SHALL run as a non-root UID/GID with a read-only-root-filesystem-compatible layout (writes only to `/data` and `/tmp`), and SHALL pass `hadolint` and a vulnerability scan with no unwaived CRITICAL findings.

#### REQ-327 — Safe content rendering · MUST
- **REQ-327.a** [U] THE SYSTEM SHALL render ingested content (messages, tool I/O, attribute values, span names) so that HTML/script payloads in it are displayed inertly. Verified by an XSS fixture whose payloads produce no script execution or remote request in the UI smoke test.

#### REQ-328 — Supply-chain checks in CI · MUST
- **REQ-328.a** [U] CI SHALL run secret scanning (gitleaks) and a dependency vulnerability audit (pip-audit) on every PR, and fail on new findings (Constitution Art. 4).

### 7.3 Availability and resilience

#### REQ-330 — Database unavailability · MUST
- **REQ-330.a** [X] IF the database becomes unreachable, THEN THE SYSTEM SHALL answer ingest with HTTP 503 / gRPC `UNAVAILABLE` (retryable), report `/readyz` as not ready, show a "storage unavailable" banner in the UI, and resume within 10 s of the database recovering, without restart.

#### REQ-331 — Graceful shutdown · MUST
- **REQ-331.a** [E] WHEN the process receives SIGTERM, THE SYSTEM SHALL stop accepting new ingest, finish committing in-flight accepted requests (bounded by a 20 s grace period), and exit 0.

### 7.4 Scalability
- Horizontal scaling applies to `collect` only, against PostgreSQL (REQ-205.c, REQ-301.b). `serve` instances behind a load balancer are out of scope in v0.1. Whether they work depends on Reflex's state backend, which would need Redis and so contradicts Constitution Art. 2. Documented as a known limitation.

### 7.5 Self-observability

#### REQ-340 — Health endpoints · MUST
- **REQ-340.a** [U] THE SYSTEM SHALL expose `GET /healthz` (process alive, always 200 unless shutting down) and `GET /readyz` (200 only when the database is reachable, the schema is current, and the ingest pipeline is accepting), in both `serve` and `collect`. Neither requires authentication, and neither reveals versions or configuration.

#### REQ-341 — Structured logs · MUST
- **REQ-341.a** [U] THE SYSTEM SHALL emit JSON-structured logs (timestamp, level, logger, message, event fields) on stdout, with level configurable, subject to REQ-323.

#### REQ-342 — Opt-in self-telemetry · SHOULD
- **REQ-342.a** [O] WHERE self-telemetry is enabled with an OTLP endpoint, THE SYSTEM SHALL export its own traces and metrics (ingest request latency, spans accepted/rejected/duplicated, queue depth, commit latency, query latency per query type) to that endpoint only.
- **REQ-342.b** [X] IF the configured self-telemetry endpoint resolves to this instance's own ingest endpoint, THEN THE SYSTEM SHALL refuse to start with a configuration error (no self-loop, Constitution Art. 9).
- **REQ-342.c** [U] Self-telemetry SHALL be disabled by default.

### 7.6 Quality and delivery

#### REQ-360 — Static quality gates · MUST
- **REQ-360.a** [U] CI SHALL fail on any `ruff check` or `ruff format --check` finding, any `mypy --strict` error in `ingest`/`store`/`query`, or any import-contract violation (Constitution Art. 6).

#### REQ-361 — Test matrix and coverage · MUST
- **REQ-361.a** [U] CI SHALL run the test suite on Python 3.11, 3.12 and 3.13, with a PostgreSQL service container for backend contract tests, and fail if line coverage of `ingest`+`store`+`query` is < 85 %.

#### REQ-362 — Traceability of tests · MUST
- **REQ-362.a** [U] Every MUST criterion ID in this PRD SHALL be cited by at least one test. A CI script SHALL list uncited MUST criteria and fail if any exist (exemptions listed explicitly with a reason, e.g. aspirational metrics).

#### REQ-363 — Compile/smoke tests · MUST
- **REQ-363.a** [U] CI SHALL compile and smoke-test: the standalone app, a minimal host app that embeds the pages (with conflicting state names), and the `site/` app, each serving its pages with HTTP 200 and no frontend console errors.

#### REQ-364 — Docker test · MUST
- **REQ-364.a** [U] CI SHALL build the image on every PR, run it, wait for healthy, run the REQ-210.a test, and run the `compose.postgres.yaml` stack (with TLS replaced by Caddy's internal CA) to healthy.

#### REQ-365 — Property tests · MUST
- **REQ-365.a** [U] The suite SHALL include `hypothesis` property tests for REQ-037.a (permutation and batching invariance) and REQ-038.a (duplicate invariance).

#### REQ-366 — Benchmark suite · MUST
- **REQ-366.a** [U] THE SYSTEM SHALL include a reproducible benchmark suite (dataset generator plus load driver plus query timer) that measures every §7.1 target and writes a machine-readable report. It runs on demand and on release tags. It is not a PR gate (CI runners are not the reference machine).

### 7.7 Documentation

#### REQ-380 — Repository documentation · MUST
- **REQ-380.a** [U] The repo SHALL contain: `README.md` (narrative pitch, `uvx` and Docker quickstarts, embedding snippet, screenshots, and a comparison table vs. LangSmith, Logfire, Langfuse and Phoenix on self-hosting footprint, privacy and embeddability, with sources and a "last checked" date), `CONTRIBUTING.md`, `CHANGELOG.md` (Keep a Changelog, from Conventional Commits), `CLAUDE.md` and `AGENTS.md` that point at `specs/constitution.md`.

#### REQ-381 — User documentation · MUST
- **REQ-381.a** [U] `docs/` SHALL cover: installation (uvx, Docker, Postgres topology), configuration reference (every setting, generated from the config model), security guide (auth modes, keys, redaction, CSP, retention), embedding guide, the seven instrumentation recipes, semconv mapping table (generated from code), pricing table maintenance, upgrade and migration notes.

---

## 8. Constraints and dependencies

### Technical constraints
- Stack as fixed by Constitution § "Stack constraints" (Python ≥ 3.11, Reflex, SQLModel/SQLAlchemy + Alembic, SQLite/PostgreSQL, OTel proto/SDK, silverpoint, mermaidjs, uv/hatchling).
- No required external services (Constitution Art. 2). In particular, Reflex's Redis state manager cannot be required, which limits horizontally scaled `serve` (§7.4).
- The frontend must ship pre-built in the wheel and the image (REQ-200.b, REQ-203.b). Feasibility with the current Reflex release is a Technical Design item (§12, Q7).

### Business constraints
- Solo maintainer with agent assistance: scope must be completable without a dedicated frontend engineer, which is why the UI uses Reflex plus the author's own components.
- MIT license; runtime dependencies permissive only.
- Positioning for regulated environments: privacy requirements (§7.2) are release gates, not nice-to-haves.

### External dependencies

| Dependency | Type | Owner | Status | Risk |
|---|---|---|---|---|
| OTel GenAI semantic conventions | Spec | OpenTelemetry SIG GenAI | Development/experimental status, evolving | High: attribute renames between revisions. Mitigated by pinning, mapping table and rebuild (REQ-035, REQ-042) |
| Reflex | Framework | Reflex Inc. | Stable, frequent releases | Medium: pre-built frontend packaging, background events/state API changes |
| `reflex-silverpoint-react` | Component | Author | Published | Medium: chart types needed by §6.7 may be missing (upstream issues; closest chart in the meantime) |
| `reflex-mermaidjs` | Component | Author | Published | Low/Medium: interaction (click → select span) support to verify |
| `opentelemetry-proto`, `grpcio` | Library | OTel / gRPC | Stable | Low |
| OpenInference / LangSmith OTel, Logfire, OpenAI Agents SDK & Agno instrumentations | Instrumentation (recipes only) | Third parties | Varies | Medium: attribute drift; fixtures record versions (REQ-260.b) |
| Bundled pricing data | Data | Maintainer | To be compiled | Medium: staleness. Editable table and "unpriced" instead of wrong numbers (REQ-061.c); sourcing/licensing decided in Tech Design |

## 9. User stories

Each story maps to the REQs that implement it. Its acceptance criteria are those REQs' EARS criteria.

**Epic A — Instrument and ingest**
- **US-001:** As Dana, I want to point my PydanticAI agent at agentlens with two environment variables, so that its runs appear without code changes. → REQ-001, 002, 006, 201, 250, 260
- **US-002:** As Pablo, I want ingestion to be authenticated, rate limited and back-pressured, so that a misbehaving agent cannot take the collector down or lose data silently. → REQ-006, 007, 009, 330
- **US-003:** As Pablo, I want to scale collectors on PostgreSQL, so that high-volume Sources don't affect the UI. → REQ-004, 072, 205, 301

**Epic B — Understand a run**
- **US-010:** As Dana, I want a waterfall with GenAI-aware labels and a drawer showing messages and tool I/O, so that I can see what the model saw and did. → REQ-150, 151, 152
- **US-011:** As Dana, I want the graph view and replay, so that I can follow the path my LangGraph agent took, including retries and interrupts. → REQ-033.d, 153, 154
- **US-012:** As Dana, I want runs to update live while my agent is running, so that I can debug in real time. → REQ-140, 145.c, 150.b

**Epic C — Analyze and control cost**
- **US-020:** As Dana, I want tokens and cost per run, model and Source, with unpriced models flagged, so that I trust the numbers. → REQ-060, 061, 106, 141, 171
- **US-021:** As Dana, I want to filter runs by model, tool, status, cost and text, and save those filters, so that I can find regressions quickly. → REQ-100, 101, 102, 145

**Epic D — Privacy and operations**
- **US-030:** As Pablo, I want redaction applied before storage, and a per-Project switch to drop content entirely, so that PHI/PII never lands on disk. → REQ-039, 040, 172, 322, 323
- **US-031:** As Pablo, I want retention by age and size, so that storage stays bounded and compliant. → REQ-090, 091, 092, 173
- **US-032:** As Pablo, I want a hardened image and compose files, so that I can deploy in an afternoon. → REQ-203, 205, 224, 326

**Epic E — Embed**
- **US-040:** As Rita, I want to mount agentlens pages in my Reflex app behind my existing login, so that my users see their agent runs without a second login. → REQ-190, 191, 193

**Epic F — Discover**
- **US-050:** As a visitor, I want a live demo and a 60-second quickstart, so that I can evaluate agentlens without installing anything. → REQ-230, 231, 233, 210

## 10. Wireframes (textual)

```
┌ agentlens ─ [Project ▾] [Source ▾] [Last 24h ▾] ─────────────── live ● ─┐
│ Dashboard | Runs | Sessions | Settings                                   │
├──────────────────────────────────────────────────────────────────────────┤
│ TRACE 4bf9…a1  ✔ ok  · planner-agent · 12.4 s · 18.2k tok · $0.0412      │
│ [Waterfall] [Graph] [Tokens & cost] [Raw]          ⏮ ◀ ▶ ⏭  1× ▾ replay  │
│ ┌──────────────────────────────────────┬───────────────────────────────┐ │
│ │ invoke_agent planner   ████████████  │ chat gpt-4o  · 1.2k tok $0.004│ │
│ │  chat gpt-4o            ███          │ ┌system──────────────────────┐│ │
│ │  execute_tool search     ░░██        │ │ You are a planner…         ││ │
│ │  approval send_email        ◆        │ └────────────────────────────┘│ │
│ │  chat gpt-4o                 ████    │ ┌user────────────────────────┐│ │
│ │  execute_tool send ✖           █     │ │ Book me a flight…          ││ │
│ └──────────────────────────────────────┴───────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Dashboard:** KPI row (6 cards) → 2-column grid of silverpoint charts (REQ-141.b), each with its own empty/error state.
- **Runs:** filter bar (chips plus "more filters" popover, saved filters menu) → table → "N new runs" pill (REQ-145.d).
- **Sessions:** table → Session detail with a Run timeline and cumulative chart.
- **Settings:** tabs for Projects & keys | Pricing | Redaction | Retention.

## 11. Risks and mitigations

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| GenAI semconv changes during development (renames, events → attributes) | High | High | Pinned revision, declarative mapping table, raw attributes kept, `rebuild` command (REQ-030, 035, 042) |
| Reflex cannot easily ship a pre-built frontend in a wheel / serve it from the backend | Medium | High (breaks the 60 s promise and the no-Node requirement) | Spike first in the Technical Design (Q7); fallback: image-only pre-built path, with `uvx` requiring a one-time build, documented |
| gRPC server in the same event loop as Reflex/uvicorn interferes with hot reload or performance | Medium | Medium | Technical Design Q1 decides; separate thread/loop or `collect`-only gRPC as fallback |
| SQLite write contention between ingest and UI reads | Medium | Medium | WAL mode, single writer task with group commit, read connections separate; benchmark REQ-300/303 |
| silverpoint lacks needed chart types (e.g. percentile bands, stacked bars) | Medium | Low | Upstream issues; closest chart; tracked in Technical Design Q6 |
| Large traces make Reflex UI sluggish (state size over websocket) | Medium | Medium | Server-side pagination of spans, collapsed subtrees, virtualized rendering (REQ-150.c) |
| LangGraph interrupts are not observable: OpenInference implements `on_interrupt`/`on_resume` as no-ops and marks `GraphInterrupt` OK | High (confirmed) | Medium | No interrupt-derived Approvals for LangGraph in v0.1; upstream issue filed against OpenInference; nodes and retries still shown (Tech Design DD-010) |
| Mermaid graphs unreadable for big runs | Medium | Low | Collapsing to 150 nodes, sequence-diagram fallback (REQ-153.d/e) |
| Pricing data stale or wrongly licensed | Medium | Medium | Editable, versioned table; unpriced over wrong; source and license recorded in the Technical Design |
| Scope too large for v0.1 (site + docs + 7 recipes + 5 pages) | Medium | Medium | Strict MoSCoW; SHOULD/COULD slip to v0.1.x; plan phases deliver core first |

## 12. Open questions (owner: Technical Design unless noted)

Each question comes with the product constraint the PRD imposes on its answer.

| # | Question | PRD constraint on the answer |
|---|---|---|
| Q1 | OTLP receiver inside the Reflex backend vs. separate collector process: impact on `reflex run` hot reload, gRPC in the same event loop, horizontal scaling | Both topologies are required (REQ-004). The decision is about *how* `serve` hosts gRPC and whether dev hot reload restarts ingestion. Must meet REQ-300 and REQ-306 in-process |
| Q2 | DuckDB for analytics over the SQLite file vs. plain SQLite with careful indexes | Decided by REQ-305 (1.5 s at 1M spans on SQLite) plus Constitution Art. 2. Adopt DuckDB only if plain SQLite misses the target in a spike benchmark |
| Q3 | Exact GenAI semconv revision and mapping table for LangGraph (OpenInference/LangSmith) and PydanticAI attribute names | Must satisfy REQ-035 and REQ-260. The table is published in docs (REQ-381) |
| Q4 | Live updates: polling vs. in-process pub/sub; behavior with a separate collector (PostgreSQL LISTEN/NOTIFY?) | Must meet REQ-140/306 (≤ 3 s p95) in both topologies without Redis. SQLite + separate `collect` is not a supported topology (`collect` requires PostgreSQL), unless the Technical Design finds a cheap way |
| Q5 | Frontend serving in Docker: Reflex backend serving the exported frontend vs. a Caddy sidecar | `compose.yaml` must remain a single container (REQ-204). Caddy is present in `compose.postgres.yaml` anyway (REQ-205) |
| Q6 | Which silverpoint chart types exist today and which must be requested upstream | All REQ-141.b charts must render with some silverpoint chart. Gaps become upstream issues |
| Q7 *(new)* | Can the current Reflex release ship the pre-built frontend inside the wheel and serve it from the backend on a single port? | Required by REQ-200.b and REQ-210.b. Spike before the plan is finalized |
| Q8 *(new)* | Approval semantics: which spans/attributes LangGraph and `reflex-agentkit` emit for interrupts and approvals today | Feeds the REQ-033.c mapping rules and the REQ-251 vocabulary proposal |
| Q9 *(new)* | Source and license of the bundled default pricing data | Must be redistributable under MIT-compatible terms. No runtime fetch (REQ-060.b) |
| Q10 *(new)* | API-key hash algorithm (Argon2id vs. HMAC-SHA-256 with server pepper), given a per-request verification cost at 50 req/s | Must satisfy REQ-320.a and the REQ-302 latency target (cached verification allowed, but revocation still applies within 5 s, REQ-006.c) |

## 13. Estimated timeline (indicative, single maintainer + agents)

| Phase | Estimated duration | Deliverable |
|---|---|---|
| Specs & design (this chain + spikes Q2, Q7) | 1–2 weeks | Approved specs, spike reports |
| Core: ingest + normalizer + store + query | 3 weeks | Golden + contract + property tests green; benchmarks run |
| UI: pages, embedding, live updates | 3 weeks | Smoke tests green; embedded host app |
| Delivery: CLI, Docker, compose, CI/CD | 1 week | Image on GHCR (pre-release tag) |
| Site, docs, recipes, sample dataset | 2 weeks | Site deployed; 7 recipe tests green |
| Hardening & release | 1 week | v0.1.0 on PyPI + GHCR |

---

## Constitution check

| Article | How this PRD complies |
|---|---|
| Art. 1 Standards-first | OTLP-only ingestion (REQ-001–008); raw attributes kept (REQ-030.b); declarative mapping table (REQ-035); no SDK (§5.2) |
| Art. 2 Zero-infra | SQLite default, no Node.js at runtime (REQ-071, 200.b); contract suite on both backends (REQ-070); no Redis, hence `serve` is not horizontally scaled (§7.4) |
| Art. 3 Privacy | Content stored by default with a per-Project off switch (REQ-040); redaction before persistence, fail-closed (REQ-039, 322); no content in logs (REQ-323); no outbound calls (REQ-324); bundled pricing, no fetch (REQ-060.b). **Interpretation, not exception:** the first-run key banner (REQ-201.a) is written directly to the TTY, not to the logging subsystem, and never under Docker (file instead) |
| Art. 4 Security | Hashed keys (REQ-320), limits and retryable backpressure (REQ-007, 009), CSP (REQ-193), non-root image (REQ-326), secret/dependency scanning (REQ-328), standalone UI auth with fail-closed bind check (REQ-224) |
| Art. 5 Data integrity | Decimal pricing and versioned costs (REQ-060/061); unpriced over zero (REQ-061.c); idempotency and order invariance (REQ-037, 038, 365); ns timestamps (REQ-030.a) |
| Art. 6 Architecture | Aggregates in SQL with a bounded-rows test (REQ-106.h); import contracts and mypy gates (REQ-360) |
| Art. 7 Embeddable / identity | No-I/O imports, namespaced state, host auth hook (REQ-190, 191); standalone built from the same pages (REQ-192); silverpoint/mermaidjs only (REQ-130.c) |
| Art. 8 Testing | REQ citation enforced by script (REQ-362); golden, property, contract, smoke, Docker and benchmark tests (REQ-260, 365, 070.b, 363, 364, 366); coverage gate (REQ-361) |
| Art. 9 Self-observability | `/healthz` and `/readyz` (REQ-340), JSON logs (REQ-341), opt-in self-telemetry with an anti-loop check (REQ-342) |
| Art. 10 Process | This PRD precedes the API spec; v0.2 extension points listed (§5.3); IDs stable and never recycled |

**Exceptions requested:** none.

---

## Change history

| Version | Date | Author | Changes |
|---|---|---|---|
| 0.1-draft.1 | 2026-10-30 | Ernesto Crespo (with Claude) | Initial draft |
| 1.0 | 2026-10-30 | Ernesto Crespo | Approved |
| 1.1 | 2026-10-30 | Ernesto Crespo (with Claude) | Tech Design amendments A1 (REQ-033.c approval sources, LangGraph interrupt risk), A2 (REQ-005.a, REQ-107.a metric names), A7 (REQ-250.a recipe instrumentations). A6 pending spike S1 |
| 1.2 | 2026-11-01 | Ernesto Crespo (with Claude) | Analyze A-12: REQ-191.a allows reusing the auth-hook result for events (≤ 30 s, same client token); page loads always call the hook (Delta `changes/2026-11-p6-ui-prep/`) |

## Approvals

| Role | Name | Date | Status |
|---|---|---|---|
| Product owner / Tech lead | Ernesto Crespo | 2026-10-30 | ☑ Approved |
