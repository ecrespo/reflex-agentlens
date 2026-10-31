# Constitution — reflex-agentlens

> Version 1.1 · Status: **Ratified** · Ratified: 2026-10-30 · Last amendment: 2026-10-30
> Scope: `github.com/ecrespo/reflex-agentlens` — the `reflex_agentlens` package, the CLI,
> the container image, the `site/` app, `docs/`, CI, and every artifact under `specs/` and `changes/`.
> Rigor level: **spec-anchored** (specs live in the repo and are the current truth;
> changes to shipped behavior go through Delta Specs in `changes/`).

This document holds the non-negotiable principles of the project. Every later artifact
(PRD, API Spec, Technical Design, Data Model, Plan, Tasks) ends with a *Constitution check*
against it. An exception without a written justification is a violation. `CLAUDE.md` and
`AGENTS.md` at the repo root point here so every agent session inherits these rules.

Tagline: *"See what your agent actually did."*

---

## Vocabulary

These terms mean exactly this in every artifact, in code identifiers, and in the UI.
A synonym used in place of one of these terms counts as terminology drift and is flagged by Analyze.

| Term | Meaning |
|---|---|
| **Trace** | One end-to-end agent execution as OpenTelemetry sees it: all spans sharing a `trace_id`. |
| **Span** | One OTel span: a graph node, an LLM call, a tool call, an approval, or any other unit of work. |
| **Run** | Our domain view of a Trace, with agent-level fields derived from its Spans (agent name, status, model(s), tokens, cost, duration). One Trace → one Run. |
| **Session** (a.k.a. Thread in UI copy) | A conversation grouping several Runs, derived from a session/conversation identifier attribute. |
| **Source** | One instrumented app or agent sending OTLP, identified by its OTel `service.name` (and optional `service.namespace`). |
| **Project** | A grouping of Sources with its own API key(s), settings, redaction rules and retention. A Project is a grouping, **not** a tenant isolation boundary. |
| **Derived view** | A row computed from Spans by the normalizer: `Run`, `Session`, `LLMCall`, `ToolCall`, `Approval`. Derived views can always be rebuilt from stored Spans. |

## Positioning

Self-hosted observability for AI agents, built with Reflex. Agentlens ingests OpenTelemetry
GenAI traces from any OTLP source (LangGraph, PydanticAI, OpenAI Agents SDK, Agno, plain
OTel SDK, `reflex-agentkit`, `reflex-mcphost`) and shows live run timelines, graph replay,
tool-call inspection, and token and cost analytics. It is pure Python, runs on the
operator's own infrastructure, can be embedded as pages in a host Reflex app, and can be
deployed as a Docker service.

### Non-goals

These are out of scope. A requirement, task or PR that drifts into any of them is out of
scope and needs a constitutional amendment first.

1. **Not an agent framework, not a prompt-management tool, not a proxy/gateway** for LLM calls.
2. **Not a general APM.** Traces without `gen_ai.*` attributes are stored and listed, but get no agent-specific views.
3. **No proprietary SDK, decorators or per-framework adapters** in v0.x. Instrumentation is OpenTelemetry's job; we ship *recipes* (docs + tested fixtures), not client code.
4. **No evaluation/judging engine in v0.1.** Annotations and dataset export arrive in v0.2.
5. **No multi-tenant SaaS features** (billing, organizations, cross-tenant isolation guarantees).
6. **No hard dependency on sibling projects** (`reflex-agentkit`, `reflex-mcphost`) in either direction. The only coupling is a documented attribute vocabulary.

---

## Articles

### Art. 1 — Standards-first ingestion
THE SYSTEM SHALL accept telemetry only through OpenTelemetry OTLP (HTTP protobuf, HTTP JSON,
gRPC). Its agent domain model SHALL be derived from the OpenTelemetry GenAI semantic conventions
at a revision pinned in the Technical Design and recorded in code as a constant. Every
attribute received, whether known or unknown to the pinned revision, SHALL be persisted raw
(as JSON) next to the normalized fields. Framework-specific attribute names that diverge from
the spec SHALL be handled only through a declarative mapping table, never through
framework-specific code paths.
*Rationale: the conventions are still evolving. Keeping the raw data plus a pinned, explicit mapping lets us re-derive views when the spec moves, without losing data or forking per framework.*

### Art. 2 — Zero-infra by default, portable storage
THE SYSTEM SHALL run collector, storage and UI in a single process with an embedded SQLite
database, with no external service required (`uvx reflex-agentlens` must work on a clean
machine). Every storage capability SHALL go through the storage protocol and pass the shared
contract test suite on **both** SQLite and PostgreSQL before merge. Schema changes SHALL ship
only as Alembic migrations tested on both backends. THE SYSTEM SHALL NOT require Redis, Kafka,
ClickHouse, a message broker or an external cache at any point in v0.x. Optional backends behind
the protocol (e.g. ClickHouse in v0.2) are allowed. Required ones are not.
*Rationale: "try it in 60 seconds" and on-prem adoption both die the moment a second piece of infrastructure becomes mandatory.*

### Art. 3 — Local-first privacy
THE SYSTEM SHALL store GenAI message content (prompts, completions, tool arguments and results)
by default, and SHALL let operators disable content storage per Project. Redaction rules SHALL be
applied **before** any persistence, so unredacted content never touches disk, the database or a
queue. Prompt/completion content and API keys SHALL NOT appear in server logs, error messages,
exception traces or self-telemetry. THE SYSTEM SHALL make no outbound network call that the
operator did not configure: no phone-home, no usage analytics, no remote pricing fetch, no
CDN-loaded assets at runtime in the standalone app.
*Rationale: this is the reason to exist for regulated environments (health, finance, public sector). One leak through a log line voids the pitch.*

### Art. 4 — Security at the edge
THE SYSTEM SHALL authenticate every ingest request with a per-Project API key, SHALL store API
keys only as salted hashes (shown in plaintext once, at creation), and SHALL enforce payload-size
limits, rate limits and schema validation on every inbound OTLP request before it reaches the
normalizer. Under overload it SHALL reject with OTLP-compliant retryable responses (HTTP 429/503,
gRPC `RESOURCE_EXHAUSTED`/`UNAVAILABLE`) instead of dropping data silently. Embedded pages SHALL
ship a documented Content-Security-Policy. The container image SHALL run as a non-root user.
Secrets SHALL come only from environment variables or files mounted at runtime, never from the repo
or the image. CI SHALL run secret scanning and dependency vulnerability auditing on every PR.
*Rationale: an ingest endpoint is an unauthenticated write path until proven otherwise; the image ends up in hospitals.*

### Art. 5 — Data integrity
THE SYSTEM SHALL store timestamps as UTC (span times as integer Unix nanoseconds, as OTLP sends
them) and SHALL represent money and token prices with `Decimal` (Python) and fixed-precision
`NUMERIC` (SQL), never float. A model missing from the pricing table SHALL yield cost **"unpriced"**
(NULL plus a flag), never 0. Pricing-table edits SHALL be versioned, and each computed cost SHALL
record the pricing version it used. Ingestion SHALL be idempotent: the same span received twice
(same `trace_id` + `span_id`) results in one stored Span, and derived views SHALL converge to the same
result regardless of span arrival order.
*Rationale: wrong cost numbers are worse than missing ones. OTLP exporters retry and deliver out of order by design.*

### Art. 6 — Layered architecture, SQL-first analytics
THE SYSTEM SHALL keep the core layers `reflex_agentlens.ingest`, `.store` and `.query` free of
any import of `reflex`, `.pages` or `.components`. The UI layer SHALL reach data only through
`.query`, never by querying the database directly. Dependencies flow `pages/components → query →
store ← ingest`, enforced by an import-contract check in CI. Aggregates (counts, sums,
percentiles, group-bys) SHALL be computed in SQL, not in Python loops over rows. The core layers
SHALL pass `mypy --strict` and the whole codebase SHALL pass `ruff check` and `ruff format --check`
with zero findings.
*Rationale: a UI-free core is what makes the standalone collector, the embedded pages and future backends possible; SQL aggregates are what keep the runs page under its latency target at 100k+ spans.*

### Art. 7 — Embeddable by construction; one visual identity
Every page in `reflex_agentlens.pages` SHALL be importable and mountable in a host Reflex app via
`app.add_page(...)` with no import-time side effects (no DB connection, no thread or server start on
import), with namespaced state and routes, and with authentication delegated to a host-provided hook.
The standalone app SHALL be built only from the same exported pages plus a shell; no page exists
only in standalone mode. All charts SHALL use `reflex-silverpoint-react` and all graph views SHALL
use `reflex-mermaidjs`. No other charting or graph-rendering dependency is permitted. A chart type
silverpoint lacks SHALL be filed as an upstream silverpoint issue and replaced with the closest
available silverpoint chart.
*Rationale: if embedding is retrofitted, standalone-only shortcuts creep in. A single charting stack keeps the identity and the dependency tree small.*

### Art. 8 — Test-first, traceable verification
For `ingest`, `store` and `query`, THE TEAM SHALL write the failing test before or in the same
commit as the code that makes it pass. Every EARS criterion of a MUST requirement SHALL be verified
by at least one automated test that cites its `REQ-NNN` (in the test name or docstring). Line
coverage on `ingest`, `store` and `query` SHALL be ≥ 85 % and enforced in CI. The suite SHALL include
normalizer golden tests from recorded OTLP fixtures (one per instrumentation recipe), property tests
for out-of-order and duplicate ingestion, storage contract tests on both backends, aggregate tests on
known datasets, compile/smoke tests for the standalone app, an embedded page and `site/`, and a Docker
build + healthcheck test. Performance targets stated in the PRD SHALL be measured by a reproducible
benchmark, not asserted.
*Rationale: EARS criteria map almost 1:1 to tests. That only pays off if the mapping is enforced, and the normalizer is where silent wrongness hides.*

### Art. 9 — Observable, but never self-referential
THE SYSTEM SHALL expose `/healthz` (liveness) and `/readyz` (readiness, including DB reachability)
and SHALL emit structured JSON logs. Self-telemetry (OTel traces/metrics about agentlens's own ingest
latency, queue depth and query timings) SHALL be **off by default**. When enabled, it SHALL export
only to an operator-configured endpoint, SHALL never be routed into agentlens's own ingest pipeline,
and SHALL obey Art. 3 (no content).
*Rationale: operators need to see the collector's health, but a collector tracing its own ingestion into itself is a feedback loop.*

### Art. 10 — Spec-anchored process
THE TEAM SHALL obtain an approved spec before implementing any work at or above feature size, and
SHALL update the affected spec as part of the Definition of Done. Changes to shipped behavior SHALL
enter as Delta Specs under `changes/<yyyy-mm>-<slug>/` and be folded into `specs/` on approval. The
Analyze gate SHALL run before implementation of any feature-size work. Branches SHALL be cut from
`develop` and merged into `develop` via PR. Releases SHALL be merged into `main` and tagged
`vX.Y.Z` (SemVer, starting at 0.1.0). Commits SHALL follow Conventional Commits, and `CHANGELOG.md`
SHALL be derived from them. All artifacts, code, comments, docs and commit messages SHALL be in English.
*Rationale: the spec is the cheapest place to be wrong. Conventional Commits plus tags make releases and the changelog mechanical.*

---

## Stack constraints

Decided; not re-discussed per feature. Exact versions are resolved and pinned in the
Technical Design. "verify" means checking the current stable release at that time.

| Area | Decision |
|---|---|
| Language | Python ≥ 3.11. CI matrix: 3.11, 3.12, 3.13 |
| Packaging | `src/` layout, `hatchling` build backend, `uv` for envs/locking, PyPI `reflex-agentlens` via trusted publishing |
| UI framework | Reflex (current stable, verify + pin). The OTLP/HTTP receiver mounts on Reflex's Starlette backend |
| Charts / graphs | `reflex-silverpoint-react` / `reflex-mermaidjs` (same author, verify APIs + pin) |
| Telemetry protocol | `opentelemetry-proto`, `opentelemetry-sdk` (pin); `grpcio` for OTLP/gRPC |
| Persistence | SQLModel / SQLAlchemy + Alembic; SQLite with FTS5 (default), PostgreSQL (production). DuckDB is an open question for the Technical Design and is not adopted until decided there |
| Quality | `ruff` (lint + format), `mypy --strict` on core layers, `pytest`, `pytest-asyncio`, `hypothesis` (property tests), an import-contract checker (e.g. `import-linter`) |
| Delivery | Multi-stage `Dockerfile` (non-root, healthcheck); `compose.yaml` (single container + SQLite volume); `compose.postgres.yaml` (app + collector + PostgreSQL + Caddy/TLS); image `ghcr.io/ecrespo/reflex-agentlens` pushed on tag |
| CI | GitHub Actions: lint, type-check, tests (matrix + Postgres service container), import contracts, secret scan, dependency audit, Docker build + healthcheck |
| Docs / site | MkDocs Material under `docs/`; `site/` is a separate Reflex app with a read-only live demo |
| Licensing | Project is MIT. Runtime dependencies SHALL be under permissive licenses compatible with MIT (MIT, BSD, Apache-2.0, ISC, PSF) |
| Forbidden in v0.x | Any *required* Redis/Kafka/ClickHouse/broker/external cache; any charting library other than silverpoint; any graph renderer other than mermaidjs; per-framework SDK adapters |

## Versioning horizon

- **v0.1 (MVP):** fully specified and implemented under this constitution.
- **v0.2 (designed for, not implemented):** annotations and dataset export, a PydanticAI evals
  runner, webhook alerts, prompt-version comparison and A/B dashboards, an optional ClickHouse
  backend, OTLP logs ingestion. v0.1 artifacts SHALL name the extension point each of these
  plugs into.

## Amendments

Amendments require a PR that changes this file, a row in the table below, and a version bump
(minor for a clarification, major for an article that is added, removed or inverted).

| Date | Article | Change | Reason | Approved by |
|---|---|---|---|---|
| 2026-10-30 | All | v1.0 ratified | Initial constitution | Ernesto Crespo |
| 2026-10-30 | Stack constraints | v1.1 (clarification): Reflex backend is Starlette, not FastAPI | Reflex 0.10 dropped FastAPI (Tech Design A3) | Ernesto Crespo |
| 2026-10-30 | Art. 5 | Interpretation recorded: money stored as fixed-point `BIGINT` nano-USD on both backends (SQLite has no exact NUMERIC); always `Decimal` in Python | Approved with Data Model N3 | Ernesto Crespo |
| 2026-10-30 | Art. 7 | Interpretation recorded: the trace waterfall / replay timeline is a tabular tree component, not a chart | Approved with Tech Design DD-017 | Ernesto Crespo |

## Constitution check (to be appended to every artifact)

Each PRD, API Spec, Technical Design, Data Model, Plan and Tasks file ends with a short section
listing the articles that apply and how they are satisfied, or the exception requested, why,
and its expiry. Analyze treats a missing check, or an exception with no justification, as a
**High** finding.
