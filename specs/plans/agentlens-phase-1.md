# reflex-agentlens v0.1 — Implementation Plan

## Metadata

| Field | Value |
|---|---|
| **Author** | Ernesto Crespo (`ecrespo`) |
| **Status** | `APPROVED` |
| **Version** | 1.0 |
| **Date** | 2026-10-31 |
| **PRD** | [`specs/prd/agentlens-v0.1.md`](../prd/agentlens-v0.1.md) v1.1 |
| **API Spec** | [`specs/api/agentlens-api-v1.md`](../api/agentlens-api-v1.md) v1.1 |
| **Tech Design** | [`specs/technical/agentlens-architecture.md`](../technical/agentlens-architecture.md) v1.1 |
| **Data Model** | [`specs/data-model/agentlens-schema.md`](../data-model/agentlens-schema.md) v1.0 |
| **Governed by** | [`specs/constitution.md`](../constitution.md) v1.1 |

Conventions:

- Task IDs are `P<phase>-<nn>`. The Tasks artifact (`specs/tasks/agentlens-tasks.md`) breaks each
  plan task into agent-executable steps that keep the same prefix (`P3-04.2`, …).
- Every task cites the `REQ-NNN` / `DD-NNN` / Data Model query (`Q<n>`) it implements. A task
  without a citation is an orphan and Analyze flags it.
- Estimates are in maintainer working days (d) and **already include the ×1.5 correction**
  (guide §5). Every task includes its own tests (Constitution Art. 8: failing test first or in the
  same commit for `ingest`, `store`, `query`).
- "Owner" is Ernesto for all tasks; "agent" marks tasks well suited to delegation to a coding agent
  working from the Tasks file. The plan assumes one maintainer with agent assistance.

---

## 1. Implementation summary

v0.1 is built in **eleven phases (P0–P10)**. Two spikes come first (P0) because they decide
designs that later phases depend on: S1 (pre-built Reflex frontend served from an installed wheel)
and S2 (plain-SQL performance). Then the layers are built bottom-up, following the import contract
(`core → store/ingest → query → app/pages`). This puts the parts that can be tested without the UI
(normalizer, store, query) under contract, golden and property tests before any page exists. The UI
(P6) consumes a finished `query` layer. Packaging and Docker (P7) reuse the S1 recipe. Recipes,
dataset and benchmarks (P8) and docs/site (P9) run partly in parallel with P6–P7. P10 closes the
release gates.

| Phase | Name | Estimate | Can run in parallel with |
|---|---|---|---|
| P0 | Spikes S1 + S2 | 11.5 d | — |
| P1 | Foundation and `core` | 8.5 d | — (P0-01 shared) |
| P2 | Store | 16 d | P3-01…P3-05 (pure functions, no DB) |
| P3 | Ingest | 21 d | P2 (see above) |
| P4 | Query | 9 d | P5-01, P5-04 |
| P5 | Runtime, CLI, ops | 8.5 d | P4, P6-01…P6-03 |
| P6 | UI | 21.5 d | P8, P9-01…P9-03 |
| P7 | Packaging and delivery | 7 d | P8 |
| P8 | Recipes, dataset, benchmarks | 11.5 d | P6, P7 |
| P9 | Docs, site, ecosystem | 9 d | P6–P8 |
| P10 | Hardening and release | 5.5 d | — |
| | **Total effort** | **≈ 129 d** | Critical path ≈ 95–105 d (§4) |

**Total estimated duration:** ≈ 95–105 working days on the critical path (≈ 20 weeks) for one
maintainer with agent assistance. Delegating P8/P9 to agents in parallel with P6 is what brings the
duration below the total effort.
**Team:** 1 maintainer (backend, frontend, ops, QA) + coding agents driven by the Tasks file.
**Target date for 0.1.0:** to be set by the maintainer at approval of this plan (proposed: decide
after P0, when the spike results firm up the P2/P6/P7 estimates).

**Release milestones** (Tech Design §9.1):

| Milestone | Gate |
|---|---|
| M0 — Spikes closed | S1 and S2 reports appended to Tech Design §11; A6, DD-018 fallback and rollup decision recorded |
| M1 — Core under contract | P1–P4 done: store contract suite green on SQLite + PG 15/18, golden + property suites green, coverage ≥ 85 % |
| M2 — `0.1.0a1` | P5–P7 done ("Core + UI"): wheel boots without Node, image healthy, compose test passes. Pre-release on PyPI + GHCR |
| M3 — `0.1.0` | Every MUST criterion cited by a passing test (REQ-362), benchmark report meets PRD §7.1 on the reference machine, P10 done |

---

## 2. Prerequisites

| Prerequisite | Owner | Status | Needed by |
|---|---|---|---|
| Constitution, PRD, API Spec, Tech Design, Data Model approved | Ernesto | ☑ Done (2026-10-30) | P0 |
| This plan approved, then Tasks + Analyze approved | Ernesto | ☐ Pending | P0 start |
| GitHub repo `ecrespo/reflex-agentlens` with `develop` (default for PRs) and `main` | Ernesto | ☑ Exists (`origin` set, `develop` checked out) | P1-03 |
| Branch protection on `develop`/`main` (required CI checks) | Ernesto | ☐ Pending | P1-03 |
| Local toolchain: `uv`, Docker + buildx, Bun 1.4.4 (build only), Playwright Chromium, PostgreSQL 15 and 18 (containers) | Ernesto | ☐ Verify | P0-02 |
| PyPI project `reflex-agentlens` with a trusted publisher for the release workflow | Ernesto | ☐ Pending | P7-05 |
| GHCR package `ghcr.io/ecrespo/reflex-agentlens` and workflow permissions | Ernesto | ☐ Pending | P7-05 |
| Reference machine (4 vCPU x86-64, 8 GiB, NVMe, Linux) for REQ-300–307 | Ernesto | ☐ Pending | P8-06 |
| **Q-T1**: hosting target for `site/` (the seraph.to stack) | Ernesto | ☐ **Open** | P9-05 only |
| API keys for recording real recipe fixtures (OpenAI-compatible endpoint or fake LLM server per recipe) | Ernesto | ☐ Decide (fake LLM preferred, REQ-260.b) | P8-03 |

---

## 3. Implementation phases

---

### Phase 0: Spikes S1 and S2

**Duration:** 11.5 d
**Objective:** Retire the two **[spike]** risks of Tech Design §11 before the dependent designs are
built. Spike code lives on `spike/s1-*` and `spike/s2-*` branches and is **not** merged into `src/`.
Reusable parts (build hook prototype, dataset generator, bench harness) are re-implemented properly
in P7-01, P8-02 and P8-05.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P0-01 | Minimal scaffold for the spikes: `pyproject.toml` (hatchling, `src/` layout), uv workspace, `uv.lock`, ruff, pytest. Becomes the base of P1-01 | DD-022 | 0.5 d | — | ☐ |
| P0-02 | **S1a** Build-only `_webapp` module with one page using a silverpoint chart, a `reflex-mermaidjs` graph and a Pydantic DTO as a state var. `reflex export --frontend-only --no-zip` packaged into the wheel by a prototype hatchling build hook | DD-002, DD-017 | 1.5 d | P0-01 | ☐ |
| P0-03 | **S1b** Boot the installed wheel with **no `node`/`bun` on PATH**: workdir copy to `{data_dir}/web/<version>/`, minimal `rxconfig.py`, `REFLEX_*` / `__REFLEX_*` env vars, Granian single worker. Determine whether `env.json` is runtime-fetched and can be rewritten for a non-8000 port | DD-002, REQ-200.b, A6 | 1.5 d | P0-02 | ☐ |
| P0-04 | **S1c** `grpc.aio` server on a dedicated thread with its own asyncio loop next to Granian; Starlette `/v1/traces` route handing off via `run_coroutine_threadsafe`; SIGTERM ordering | DD-001, REQ-004.a | 1 d | P0-03 | ☐ |
| P0-05 | **S1d** Playwright run: CSP header with build-time `sha256` hashes enforced, zero third-party requests (Mermaid `font_awesome=False`, `icon_packs=[]`; silverpoint fonts), no console errors. Check whether inline scripts are stable per request | DD-018, REQ-193, 324.b | 1 d | P0-03 | ☐ |
| P0-06 | **S1 report** appended to Tech Design §11. Decide DD-002 consequence 1 (rewrite `env.json` vs. fixed port), DD-018 fallback, and apply or drop Amendment A6 (PRD §7.4) | DD-002, DD-018, A6 | 0.5 d | P0-04, P0-05 | ☐ |
| P0-07 | **S2a** Prototype of the hot tables (`spans`, `runs`, `llm_calls`, `tool_calls`, `graph_nodes`, `span_search`, `sessions`) from the Data Model on SQLite and PG; synthetic generator for 100k and 1M spans (~20 spans/Run, sample-dataset distributions) | Data Model §2.8–2.15 | 2 d | P0-01 | ☐ |
| P0-08 | **S2b** Write-path benchmark: group commit (≤ 50 ms / ≤ 5,000 spans), insert-or-ignore `RETURNING` (Q6) and set-based Run/Session recompute (Q7). Measure spans/s against REQ-300 (SQLite) and REQ-301 (PG) | DD-003, DD-004 | 1.5 d | P0-07 | ☐ |
| P0-09 | **S2c** Query benchmark: Q1–Q3 on 100k, Q5 on a 1,000-span trace, Q8 + Q10 (with `percentile_sql()`) on 1M, both backends | DD-005, REQ-303–305 | 1.5 d | P0-07 | ☐ |
| P0-10 | **S2 report** appended to Tech Design §11. Decide whether `rollup_hourly` (Data Model §2.19, ladder step 1) is adopted. If adopted, fold it into `0001_initial` before P2-01 | DD-005, Data Model §2.19 | 0.5 d | P0-08, P0-09 | ☐ |

#### Done criteria
- Both reports are in Tech Design §11 with the measured numbers, the machine they ran on, and a
  decision for every question in the spike table.
- A6 is either applied to PRD §7.4 (PRD v1.2) or recorded as not needed.
- If S1 fails outright (the wheel cannot boot without a JS runtime), stop and open a Delta Spec
  before P6/P7. REQ-200.b cannot be met as designed and needs a decision from the maintainer.
- If S2 misses REQ-305 even with rollups, stop and open a Delta Spec (ladder step 2: re-evaluate
  DuckDB).

---

### Phase 1: Foundation and `core`

**Duration:** 8.5 d
**Objective:** Repository, quality gates and CI in place. The leaf package `core` is complete and
tested, so every later layer starts against enforced contracts.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P1-01 | Repository layout per Tech Design §5.1: package skeleton (`core`, `ingest`, `store`, `query`, `components`, `pages`, `app`, `cli`, `data`, `_webapp`), uv workspace members `site/` and `examples/` (placeholders), dependency groups `dev`/`docs`/`bench`, runtime pins per Tech Design §9.3, `_frontend/` gitignored | DD-022 | 1 d | P0-01 | ☐ |
| P1-02 | Quality gates: ruff (lint + format), `mypy --strict` on `core`/`ingest`/`store`/`query`, `import-linter` contracts (layers + forbidden list of Tech Design §5.1), pytest + coverage with the 85 % gate on `ingest`+`store`+`query` | REQ-360, 361, Art. 6 | 1 d | P1-01 | ☐ agent |
| P1-03 | CI workflows (GitHub Actions, actions pinned by SHA, `astral-sh/setup-uv`): `lint`, `types`, `test` (3.11/3.12/3.13 × SQLite; 3.13 × PG 15 and 18 service containers), `security` (gitleaks, `uvx pip-audit`). Later phases add `frontend-build`, `smoke`, `docker`, `docs` | REQ-328, 361 | 1 d | P1-02 | ☐ agent |
| P1-04 | Repo docs: `CLAUDE.md` and `AGENTS.md` pointing at `specs/constitution.md`, `CONTRIBUTING.md` (branching from `develop`, Conventional Commits, spec-anchored process), `CHANGELOG.md` skeleton (Keep a Changelog) | REQ-380 (partial), Art. 10 | 0.5 d | P1-01 | ☐ agent |
| P1-05 | `scripts/check_req_coverage.py`: parse MUST criteria from the PRD, find citations in `tests/`, explicit exemptions file with reasons. Wired into CI as **report-only** until P10-02 makes it blocking | REQ-362 | 1 d | P1-02 | ☐ agent |
| P1-06 | `core.errors` (API §6.6 hierarchy), `core.ids`, `core.timeutil` (UTC, Unix ns), `core.money` (`Decimal` ↔ nano-USD `BIGINT`), `core.security` (key generation with `al_` prefix and ≥ 128-bit CSPRNG, salted SHA-256 + `compare_digest`, HMAC signing for cookies and cursors) | API §6.6, REQ-320.b, Art. 5, DD-014 | 1.5 d | P1-02 | ☐ |
| P1-07 | `core.config`: pydantic-settings models for every section of API §8.2 (incl. A4 `public_url`, `ingest_enabled`), source chain defaults → TOML → env → CLI, unknown-key detection with source naming, `*_FILE` secrets, masked `--print-config` rendering. Pure, no I/O at import | REQ-202, DD-019 | 1.5 d | P1-06 | ☐ |
| P1-08 | `core.logging`: JSON formatter, content guard filter (allowlisted extra keys of Tech Design §7.1, exception message → class name for normalizer/redaction exceptions, no locals) | REQ-341, 323.a, DD-020 | 1 d | P1-06 | ☐ |

#### Done criteria
- CI green on `develop` with all jobs above. Import contracts fail a deliberately bad import in a
  test branch.
- `core` coverage ≥ 85 % and `mypy --strict` clean.
- `CLAUDE.md`/`AGENTS.md` present, so later agent sessions inherit the constitution.

---

### Phase 2: Store

**Duration:** 16 d
**Objective:** The `StorageBackend` protocol with SQLite and PostgreSQL implementations, migration
`0001_initial`, and the contract suite green on SQLite, PG 15 and PG 18.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P2-01 | `store.schema` (SQLAlchemy Core) for Data Model §2.1–2.18 (+ §2.19 if P0-10 adopted it), physical type mapping §2.0, attribute JSON encoding §1.4; `store.records` (row dataclasses) | Data Model §2, DD-008 | 1.5 d | P0-10, P1-06 | ☐ |
| P2-02 | Alembic (async `env.py`, `render_as_batch`), revision `0001_initial`, `schema_status` (CURRENT / OUTDATED / NEWER), auto-migrate, PG advisory-lock migration with waiters; startup checks SQLite ≥ 3.43 + FTS5, PG ≥ 15 | REQ-074, Data Model §5, DD-008 | 1.5 d | P2-01 | ☐ |
| P2-03 | `store.protocol` (typed `StorageBackend`, API §5) and `store.dialect.SqlDialectOps` (`ON CONFLICT`, JSON ops, `percentile_sql()` nearest-rank, FTS query builder with token whitelist) | REQ-070.a, DD-005, DD-006, REQ-325.b | 1 d | P2-01 | ☐ |
| P2-04 | Contract-suite harness: backend-parametrized fixture, PG via `AGENTLENS_TEST_PG_URL`, contract dataset builder, helper asserting rows fetched per aggregate | REQ-070.b, 106.h | 1 d | P2-03 | ☐ |
| P2-05 | SQLite backend: engines (single WRITE connection, READ pool), WAL + `synchronous=NORMAL` + `busy_timeout`, `write_batch` (Q6 insert-or-ignore + Q7 recompute in one transaction, API §5.1), FTS5 contentless table | REQ-071, 038, 037.a, 073.a, DD-003, DD-004 | 2 d | P2-02, P2-04 | ☐ |
| P2-06 | PostgreSQL backend: `asyncpg`, `write_batch` with `RETURNING`, `tsvector` + GIN search, multi-writer contract test (two writers, overlapping traces, duplicates) | REQ-072, 073.b, 205.c | 2 d | P2-05 | ☐ |
| P2-07 | Read methods on both backends: Q1–Q5 (runs keyset, filters, attribute and text filters, resolve trace, trace detail), Q8–Q11 (KPIs, sessions, dashboard aggregates, metric token usage), each with a bounded-rows test | REQ-100–107 (storage side), 106.h, 303–305 shape | 2 d | P2-05, P2-06 | ☐ |
| P2-08 | Settings storage: Projects, API keys (Q14 lookup by `key_id`), redaction rule sets, pricing versions/entries (versioned, immutable once saved), saved filters, `app_meta` | REQ-170 (storage), 060.a/c, 102.a, 320.a | 1.5 d | P2-05 | ☐ |
| P2-09 | Retention: Q13 purge by age and by size in bounded batches, Session recompute after purge, `purge_log`, `storage_usage` (estimated + physical bytes), `job_lock` (advisory lock on PG, in-process on SQLite) | REQ-090, 091, 092.b/c | 1.5 d | P2-07 | ☐ |
| P2-10 | `store.changefeed`: thread-safe in-process broadcaster, PG `pg_notify` inside the commit transaction (≤ 7,900-byte payload, `truncated`), LISTEN connection with `origin_id` filtering, polling fallback on lost connection | REQ-140.a (store side), DD-007, API §5.2 | 1.5 d | P2-06 | ☐ |
| P2-11 | Contract-suite completion: identical results on both backends for runs, search sets (REQ-073.b), percentiles, Sessions; migrations up on empty DBs for both backends in CI | REQ-070.b, 072.a, 073.b, 074 | 0.5 d | P2-07…P2-10 | ☐ |

#### Done criteria
- Contract suite green on SQLite, PG 15 and PG 18 in CI.
- `0001_initial` applies cleanly on empty databases; NEWER/OUTDATED detection tested.
- `store` coverage ≥ 85 %, `mypy --strict` clean, no import of `ingest`/`query`/`reflex`.

---

### Phase 3: Ingest

**Duration:** 21 d
**Objective:** OTLP over HTTP and gRPC through admission, normalizer, redaction, cost and the
group-commit writer, with golden, property and integration tests. P3-01…P3-05 are pure functions
and can start in parallel with P2.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P3-01 | `core.semconv`: constants (`SEMCONV_CORE = 1.44.0`, `SEMCONV_GENAI_COMMIT` pinned → **closes Q-T2**), mapping table `MAPPING_VERSION = 1` exactly as Tech Design §5.3 + §5.4, dialect resolution (spec wins, REQ-035.b), `messages` normalizer (attribute, event and OpenInference flattened forms → `Message` DTO) behind an input interface ready for log records | REQ-035, 034, Art. 1, DD-009 | 2 d | P1-06 | ☐ |
| P3-02 | Normalizer, part 1: decode (protobuf via upb, JSON via `json_format`), span validation (REQ-010 rules, API §3.6), value truncation with marker and flag, Source identification (`service.name` + namespace, `unknown_service`), span classification | REQ-001.c, 002.a, 010, 030, 031, 041 | 2 d | P3-01 | ☐ |
| P3-03 | Normalizer, part 2: derive LLMCall, ToolCall, Approval (agentkit/mcphost vocabulary, PydanticAI deferral → `pending`, MCP elicitation COULD), GraphNode (spec agents, OpenInference `graph.node.*`, LangGraph metadata), token totals per DD-012, search document (≤ 32 KiB) | REQ-033, 032.d, 036.c, 073.a, DD-010, DD-011, DD-012 | 2 d | P3-02 | ☐ |
| P3-04 | `ingest.redaction`: presets (`secrets`, `email`, `phone`, `credit_card` with Luhn), pattern and field rules, `regex` timeout fail-closed, JSON-string attributes redacted leaf by leaf, hook (`module:function`, raise → reject span), content drop per Project, `preview` (used by Settings) | REQ-039, 040, 172.b, DD-013 | 2 d | P3-02 | ☐ |
| P3-05 | `ingest.cost` + bundled pricing: `scripts/update_pricing.py` (dev-time, LiteLLM JSON → `data/pricing.csv`, license verified, `NOTICE` attribution), loading as pricing version 1, matching order (response before request model, exact before longest prefix/glob), `Decimal` cost, unpriced as NULL + flag, cache-write price (COULD) | REQ-060, 061, Art. 5, DD-016 | 1.5 d | P3-03 | ☐ |
| P3-06 | `ingest.writer` + `WriteQueue`: byte budget (default 256 MiB), linger ≤ 50 ms / ≤ 5,000 spans, futures resolved after commit, commit-latency tracking for admission, `StorageUnavailable` on failure, `ping()` retry with backoff 0.5 → 5 s | REQ-009, 330, DD-003 | 1.5 d | P2-05, P3-03 | ☐ |
| P3-07 | `ingest.engine`: dedicated thread + loop, `submit()` across loops, supervisor with restart backoff (max 5 per 10 min), jobs: finalizer (Q12, every minute), purge schedule (`job_lock`), settings-cache refresh; ChangeFeed publish after commit | REQ-032.b/c, 037.c, 092.a, DD-001 | 1.5 d | P3-06, P2-09, P2-10 | ☐ |
| P3-08 | `ingest.admission` + `limits`: key parsing (`X-Agentlens-Key` > `Authorization: Bearer`), positive auth cache ≤ 5 s cleared on `settings_changed`, per-Project request and span token buckets with `Retry-After`, Content-Length precheck, streaming gzip with decompressed cap | REQ-006, 007, 320.a, DD-014, DD-015 | 1.5 d | P2-08, P3-07 | ☐ |
| P3-09 | `ingest.http` (Starlette routes): `/v1/traces`, `/v1/metrics`, `/v1/logs` → 404, content negotiation (JSON in → JSON out), `partialSuccess`, error → status mapping (400/401/413/415/429/503 with `google.rpc.Status`) | REQ-001, 002, 005.a, 007, 008, 009.b, 010 | 1.5 d | P3-08 | ☐ |
| P3-10 | `ingest.grpc`: `TraceService` and `MetricsService`, `LogsService` → `UNIMPLEMENTED`, gzip, key from metadata, status mapping (`UNAUTHENTICATED`, `RESOURCE_EXHAUSTED` ± `RetryInfo`, `UNAVAILABLE`), port disabled by config | REQ-003, 006.b, 007, 008, 009.b | 1.5 d | P3-08 | ☐ |
| P3-11 | Metrics ingestion: GenAI metric allowlist (Tech Design §5.3 "Metrics persisted", A2), `metric_points`, discarded counter for non-GenAI metrics | REQ-005, A2 | 1 d | P3-09 | ☐ |
| P3-12 | Property + golden normalizer tests: `hypothesis` permutations, batch splits and duplicates of fixture span sets → identical derived views; hand-written edge fixtures (missing parent, late root, conflicting session IDs, both dialects present, generic trace) | REQ-037, 038, 365, 032.c, 036.c, 035.b, 131 (data) | 1.5 d | P3-07 | ☐ |
| P3-13 | Integration tests on real endpoints (HTTP + gRPC): auth, limits, zip bomb, backpressure 503, **SIGKILL-after-ack test**, DB-outage → 503 + recovery ≤ 10 s, revocation ≤ 5 s across two processes on PG | REQ-001–010, 006.c, 009.a, 330 | 1.5 d | P3-09, P3-10 | ☐ |

#### Done criteria
- All REQ-001…REQ-041 MUST criteria cited by passing tests (P1-05 report).
- Property suite runs in CI with a fixed `hypothesis` profile.
- No content in captured logs during the ingest test run (pre-check for REQ-323.b).
- `ingest` coverage ≥ 85 %, `mypy --strict` clean, no import of `query`/`reflex`.

---

### Phase 4: Query

**Duration:** 9 d
**Objective:** `QueryService`, `AdminService` and the subscription helper as the only door from the
UI to data (Art. 6), with authorization in one place.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P4-01 | DTOs (API §6, Pydantic v2), signed keyset cursors, `RunFilter` + Runs URL grammar (parse/serialize round-trip, field-named validation errors, never unfiltered on error) | REQ-100.a, 101, 102.b, 325.a, API §6.2 | 1.5 d | P2-07 | ☐ |
| P4-02 | `QueryService` reads: runs, `resolve_trace`, `get_trace` (tree, sibling order, subtree token/cost totals, missing-parent placeholder, pages of 2,000 nodes), `get_span_raw`, LLM calls with normalized messages, sessions list/detail | REQ-100, 103, 104, 105, 037.b, 152 (data) | 2 d | P4-01 | ☐ |
| P4-03 | Aggregates: dashboard set (KPIs, runs by status, tokens/cost by model and Source, p50/p95(/p99) per operation and per graph node, tools, approvals) and metric token usage; `includes_unpriced` everywhere | REQ-106, 107, 061.c | 1.5 d | P4-01 | ☐ |
| P4-04 | `query.graph`: `AgentGraph` DTO (nodes, parent + sequence edges, execution counts, retries, approvals, collapse above 150 nodes) | REQ-153 (data), DD-011 | 1 d | P4-02 | ☐ |
| P4-05 | `AdminService`: Projects (create with defaults, delete with name confirmation), API keys (create shows plaintext once, list, revoke), pricing versions (+ CSV import/export SHOULD, unpriced models list), redaction rules (save-time compile + adversarial sample), retention/content settings, recompute costs (SHOULD), admin gating, allowed-Project restriction, `settings_changed` event | REQ-170–174, 062, 191.c, 039.d, 039.f | 2 d | P2-08, P3-04 | ☐ |
| P4-06 | `query.subscribe`: coalescing to ≤ 1 notice/s per subscriber, per-Project filtering, demo-mode guard rejecting every mutation | REQ-140.b, 231.b, API §6.4–6.5 | 1 d | P2-10 | ☐ |

#### Done criteria
- Every REQ-100…REQ-107 MUST criterion cited by a passing test on both backends.
- Injection cases (attribute keys, FTS syntax, sort fields) pass (REQ-325.b).
- `query` coverage ≥ 85 %, no import of `ingest` except `ingest.redaction.preview`.

---

### Phase 5: Runtime, CLI and operations

**Duration:** 8.5 d
**Objective:** Everything a process needs to run outside the UI: health, lifecycle, bootstrap, CLI,
standalone auth, shutdown. `serve` gets its Reflex wiring in P6-11. Here it runs with the
operational routes only, which is enough to test the lifecycle.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P5-01 | `app.routes`: `/healthz`, `/readyz` (DB reachable, schema CURRENT, ingest accepting; no versions or config revealed); `collect` app = minimal Starlette with health + OTLP/HTTP; `collect` on SQLite → exit 2 | REQ-340, 004.b, API §4 | 1 d | P3-09 | ☐ |
| P5-02 | `app.lifespan`: migrations (auto-migrate or exit 4), engine start/stop, first-run bootstrap (`default` Project + key; TTY banner written directly, else 0600 file; `AGENTLENS_BOOTSTRAP_API_KEY` hashed and never printed) | REQ-074.a/b/c, 201 | 1.5 d | P5-01, P4-05 | ☐ |
| P5-03 | CLI (`argparse`): `serve`, `collect`, `demo` (stub until P8-02), `db upgrade`, `db status`, `rebuild`, `purge [--dry-run]`, `keys create|list|revoke`, `version`, `--print-config`; exit codes 0–5 of API §8.3; port-in-use message naming the port and setting | REQ-200.a/c, 042.a, 092.a, 202.b/d | 2 d | P5-02 | ☐ |
| P5-04 | Standalone auth: `none` / `password` (signed `HttpOnly` `SameSite=Lax` cookie, `Secure` on HTTPS, login throttle) / `proxy` (header trusted only from `trusted_proxies`, optional admin-group header); non-loopback bind without `auth.mode` → exit 2; warning for `none` on non-loopback; Origin/Referer check on login/logout; CORS pinned to `public_url` | REQ-224, 321, A4, Tech Design §6.1 | 2 d | P1-07 | ☐ |
| P5-05 | Graceful shutdown: SIGTERM closes admission (503), drains the writer within `shutdown_grace` (20 s), stops gRPC, exits 0 | REQ-331 | 0.5 d | P5-03 | ☐ |
| P5-06 | Rebuild under concurrent ingest: result identical to a rebuild with ingest stopped; reports Runs changed | REQ-042.b | 0.5 d | P5-03 | ☐ |
| P5-07 | Self-telemetry extra `[selftelemetry]`: spans and metrics of Tech Design §7.2, off by default, self-loop check → exit 2, content-free | REQ-342 (SHOULD), DD-020, Art. 9 | 0.5 d | P5-03 | ☐ agent |
| P5-08 | Topology equivalence: the golden suite runs against `serve` and `collect` and yields identical stored data | REQ-004.c | 0.5 d | P5-03, P3-12 | ☐ |

#### Done criteria
- Every CLI command has an integration test, including every non-zero exit code.
- `collect` on PG and `serve` (operational routes only) pass health, shutdown and outage tests.

---

### Phase 6: UI

**Duration:** 21.5 d
**Objective:** All pages as embeddable callables, the standalone app built only from them, and the
smoke suite (Playwright) proving compile, embedding, CSP, inert rendering and zero third-party
requests.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P6-01 | Theme and base: `silverpoint_provider` with one palette, status/kind tokens validated with `check_palette`, `AgentlensState` (namespaced), event-handler wrapper (StorageUnavailable → banner, Forbidden → state, other → generic error), inline error states, empty state with quickstart snippet | REQ-130.b/d/e, 330 (UI), Tech Design §5.5 | 1.5 d | P4-02, P0-06 | ☐ |
| P6-02 | Shared chrome: Project, Source and time-range selectors (1 h, 24 h, 7 d, 30 d, custom) persisted in the URL | REQ-130.a | 1 d | P6-01 | ☐ |
| P6-03 | Embedding API: lazy `reflex_agentlens.__init__` (`__getattr__`), `pages.*` callables, `mount(prefix, ingest=, demo=, pages=)`, auth hook protocol and `Viewer`, unauthenticated → redirect or "not authorized", allowed-Project restriction; **no-I/O-on-import test** (network and FS writes blocked) | REQ-190, 191, Art. 7 | 1.5 d | P6-01 | ☐ |
| P6-04 | Runs page: table (REQ-145.a columns), filter bar for every REQ-101 filter, saved filters, live insertion on the first page vs. "N new runs" indicator elsewhere, "generic" badge | REQ-145, 102, 131 | 2 d | P6-02, P4-06 | ☐ |
| P6-05 | Trace page: waterfall as a span-tree table with proportional bars (Art. 7 interpretation), GenAI-aware labels, kind colors, collapse beyond depth 3, "load more", live append while `in_progress`, not-found state, generic traces without GenAI panels | REQ-150, 103.b, 131, 037.b (UI) | 2 d | P6-02 | ☐ |
| P6-06 | Span detail drawer: header (name, kind, timing, status, Source), LLM messages as role bubbles, tool I/O as collapsible JSON, errors and stack traces, raw attributes tab (lazy `get_span_raw`), token and cost breakdown; Markdown with raw HTML disabled and link-scheme allowlist | REQ-151, 152, 327 | 2 d | P6-05 | ☐ |
| P6-07 | Graph view (`reflex-mermaidjs`, `flowchart LR`, `classDef taken/error/approval/retry`, `security_level="strict"`, escaped labels, node click → select span) and replay mode | REQ-153, 154, DD-011, Art. 7 | 2 d | P6-05, P4-04 | ☐ |
| P6-08 | Sessions list and Session detail (Runs in order, cumulative `area_chart`) | REQ-160, 105 | 1.5 d | P6-02 | ☐ |
| P6-09 | Dashboard: KPI cards and the silverpoint charts of DD-017's mapping, click-to-filter navigation (SHOULD), ≤ 10 s live refresh, metrics chart (COULD) | REQ-141, 106, 107, 130.c | 2 d | P6-02, P4-03 | ☐ |
| P6-10 | Settings: Projects and keys (plaintext once + copy + `OTEL_EXPORTER_OTLP_*` snippet, revoke, delete with name confirmation), pricing editor (+ CSV, unpriced models), redaction editor with preview (SHOULD) and "existing data not rewritten" notice, retention/content toggle + storage usage + last purge, admin gating, read-only demo view | REQ-170–174, 039.f, 231.b | 2.5 d | P6-02, P4-05 | ☐ |
| P6-11 | Standalone app + `serve` wiring per the S1 recipe: `standalone_app()` from exported pages + shell + standalone auth, workdir copy, `env.json` rewrite or fixed port (P0-06), CSP middleware from `_frontend/csp.json`, CORS | REQ-192, 193, 200.b, DD-002, DD-018 | 1.5 d | P6-03, P5-04, P0-06 | ☐ |
| P6-12 | Smoke suite (Playwright, CI job `smoke`): standalone app, minimal host app with colliding state names and routes, XSS fixture (no execution, no remote request), outbound request interception, CSP header test, no console errors | REQ-363 (standalone + embedded), 190.d, 193.a, 324, 327 | 1.5 d | P6-04…P6-11 | ☐ |
| P6-13 | File the four upstream silverpoint issues of DD-017 (N-series line chart, quantile box plot, time-scale axis, trace waterfall) and link them from docs | PRD §4.1 goal, DD-017 | 0.5 d | P6-09 | ☐ |

#### Done criteria
- Every page works embedded and standalone; no page exists only in standalone mode.
- Smoke suite green in CI; zero third-party requests; CSP without `unsafe-eval`.
- REQ-130…REQ-193 MUST criteria cited by passing tests.

---

### Phase 7: Packaging and delivery

**Duration:** 7 d
**Objective:** A wheel that boots without Node, a hardened image, both compose topologies tested in
CI, and the release workflow. Ends with `0.1.0a1` (milestone M2).

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P7-01 | `hatch_build.py`: run `reflex export --frontend-only --no-zip` on `_webapp`, package `_frontend/`, compute CSP hashes into `csp.json`; CI job `frontend-build` (Bun 1.4.4) + **wheel boot test with no `node`/`bun` on PATH** (≤ 30 s to UI + ingest) | DD-002, DD-018, REQ-200.b, 210.b | 1.5 d | P6-11 | ☐ |
| P7-02 | Multi-stage `Dockerfile` (uv builder + Bun; `python:3.13-slim` runtime, UID/GID 10001, `/data`, `HEALTHCHECK` via `python -c`, no toolchain), read-only-rootfs check, `hadolint` + `trivy` in CI, size check (≤ 500 MB SHOULD) | REQ-203, 326, DD-021 | 1.5 d | P7-01 | ☐ |
| P7-03 | `compose.yaml` (SQLite volume, ports bound to `127.0.0.1`, explicit `auth.mode=none`) + **60-second test** (`up` → healthy → send recorded PydanticAI fixture with bootstrap key → Run visible in run-list query ≤ 60 s) | REQ-204, 210.a, 364 | 1 d | P7-02, P8-03 | ☐ |
| P7-04 | `compose.postgres.yaml` + `Caddyfile` (TLS, `tls internal` in CI; only Caddy publishes ports; app with `ingest_enabled=false`; collectors wait for schema CURRENT) + CI test to healthy with `--scale collector=2` and no loss/duplicates | REQ-205, 364, 072.b, DD-021 | 1.5 d | P7-02 | ☐ |
| P7-05 | Release workflow on `v*` tags on `main`: `uv build`, PyPI trusted publishing, GHCR multi-arch (`amd64`, `arm64`) tags `X.Y.Z`, `X.Y`, `latest`; `CHANGELOG.md` section generated from Conventional Commits | REQ-206, Art. 10 | 1 d | P7-02 | ☐ |
| P7-06 | **Milestone M2**: `0.1.0a1` pre-release (PyPI + GHCR `:0.1.0-alpha.1`), smoke-check `uvx reflex-agentlens==0.1.0a1` on a clean machine | Tech Design §9.1 | 0.5 d | P7-01…P7-05 | ☐ |

#### Done criteria
- `docker` and `frontend-build` CI jobs green on every PR.
- `0.1.0a1` installable with `uvx` and runnable with `docker compose up`.

---

### Phase 8: Recipes, sample dataset and benchmarks

**Duration:** 11.5 d
**Objective:** The seven instrumentation recipes with recorded fixtures and golden tests, the
deterministic sample dataset (used by `demo`, the site and the 60 s test), and the benchmark suite
run on the reference machine. P8-01…P8-04 can start once P3 is done.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P8-01 | `examples/` workspace member: deterministic fake LLM server, PydanticAI example agent, LangGraph example agent (multi-tool, tool error with recovery, failure, retried node, approval approved/rejected, multi-run Session, unpriced model) | REQ-233.a/c | 1.5 d | P3-13 | ☐ agent |
| P8-02 | `scripts/generate_dataset.py`: runs the example agents, captures OTLP/JSON, seed-deterministic output with timestamps normalized; adds one generic non-GenAI trace; bundles into `data/sample_dataset/`; `demo` command loads it with timestamps shifted into the last 7 days and ingest disabled | REQ-233, 231.c, 200.a (`demo`) | 2 d | P8-01, P5-03 | ☐ |
| P8-03 | Record recipe fixtures under `tests/fixtures/otlp/` for the seven recipes of REQ-250.a (+ LangSmith OTel variant, + contrib `openai-agents-v2` SHOULD), each with framework and instrumentation versions recorded; recordings run offline against the fake LLM | REQ-250, 260.b, A7, Q-T3 | 2.5 d | P8-01 | ☐ |
| P8-04 | Golden recipe integration tests through the real HTTP endpoint asserting status, agent name, models, LLM/tool call counts, token totals, cost/unpriced, Session ID | REQ-260.a | 1.5 d | P8-03 | ☐ |
| P8-05 | Benchmark suite `tests/bench/`: dataset generator (100k / 1M spans), async load driver (512-span batches, 4 clients, 10 min), query timer for Runs/Trace/Dashboard, live-update latency probe, RSS sampling, machine-readable JSON report; workflow on release tags and on demand | REQ-366 | 2 d | P7-01 | ☐ |
| P8-06 | Run the suite on the reference machine; fix regressions or trigger the fallback ladder (rollups / `ProcessPoolExecutor` normalizer, DD-001) via Delta Spec if a target is missed | REQ-300–306, 307 (SHOULD), 301.b (SHOULD), 303.b (SHOULD) | 2 d | P8-05, prerequisite "reference machine" | ☐ |

#### Done criteria
- 7/7 recipe golden tests green offline (PRD §4.1 goal).
- Benchmark report published as a CI artifact and meets every MUST target in PRD §7.1.

---

### Phase 9: Docs, site and ecosystem

**Duration:** 9 d
**Objective:** User documentation, README, the `site/` app with the live demo, and the upstream
vocabulary proposals. Only P9-05 depends on **Q-T1**.

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P9-01 | MkDocs Material skeleton (re-check upstream maintenance status, **closes Q-T4**), `mkdocs build --strict` CI job; installation (uvx, Docker, Postgres topology); configuration reference **generated from the config model**; semconv mapping table **generated from code** | REQ-381, 232.a, Q-T4 | 2 d | P1-07, P3-01 | ☐ agent |
| P9-02 | Security guide (auth modes, keys, redaction, CSP list for embedders, retention), embedding guide (~10 lines), seven recipe pages (snippet, env vars, known divergences), pricing maintenance, upgrade and migration notes, operations (suggested alerts of Tech Design §7.3; WAL durability caveat; `collect` scaling limit) | REQ-381, 250.a, 193.a | 2 d | P6-11, P8-03 | ☐ agent |
| P9-03 | `README.md`: pitch, `uvx` and Docker quickstarts, embedding snippet, screenshots, comparison table vs. LangSmith, Logfire, Langfuse, Phoenix with sources and a "last checked" date | REQ-380 | 1 d | P7-06 | ☐ |
| P9-04 | `site/` app: landing (tagline, 60 s quickstart tabs, feature grid, screenshots/GIFs, "Why not SaaS", sibling projects, links), a silverpoint footprint chart, `CHANGELOG.md` page, live demo via `mount(demo=True, pages=…)`; site compile/smoke test | REQ-230, 231, 232.b, 363 (site) | 2.5 d | P6-12, P8-02 | ☐ |
| P9-05 | Site container image (agentlens base + site + demo SQLite generated at build) and deployment to the hosting chosen in **Q-T1** | DD-023 | 1 d | P9-04, **Q-T1 answered** | ☐ blocked |
| P9-06 | Shared vocabulary docs (Tech Design §5.4) and upstream issues in `reflex-agentkit` and `reflex-mcphost`; OpenInference issue for LangGraph interrupt/resume attributes | REQ-251, DD-010, PRD §4.1 goal | 0.5 d | P3-03 | ☐ |

#### Done criteria
- `docs` CI job green with `--strict`; config and mapping pages regenerate without diff.
- Site smoke test green; demo is read-only (mutations rejected).
- All upstream issues filed and linked from docs.

---

### Phase 10: Hardening and release

**Duration:** 5.5 d
**Objective:** Close the release gates of PRD §7.2 and §7.6 and ship `0.1.0` (milestone M3).

#### Tasks

| ID | Task | Implements | Estimate | Dependency | Status |
|---|---|---|---|---|---|
| P10-01 | Security suite: DB byte-level canary scan (SQLite file + WAL; `pg_dump`), log + self-telemetry canary with `DEBUG`, plaintext-key scan of DB and logs for every generated key, outbound-socket interception during `serve` + smoke run | REQ-320.a, 322, 323.b, 324.a | 2 d | P6-12, P5-07 | ☐ |
| P10-02 | Make `check_req_coverage.py` blocking in CI with the final exemptions list; confirm the coverage gate | REQ-362, 361 | 0.5 d | all test tasks | ☐ |
| P10-03 | Re-run Analyze (code vs. specs), fold any drift into specs or Delta Specs (Definition of Done) | Art. 10 | 1 d | P10-02 | ☐ |
| P10-04 | Maintainer UAT against the user goals of PRD §4.2 (Dana, Pablo, Rita) on the `0.1.0rc` build | PRD §4.2 | 1 d | P10-03 | ☐ |
| P10-05 | **Milestone M3**: release `0.1.0`: merge `develop` → `main`, tag `v0.1.0`, verify PyPI + GHCR, `uvx reflex-agentlens` on a clean machine, release notes with pricing snapshot date | REQ-206, 060.b, 210.b | 1 d | P10-04, P8-06 | ☐ |

#### Done criteria
- Every MUST criterion cited by a passing test, with no unexplained exemption.
- Benchmark report meets PRD §7.1. No CRITICAL/HIGH unwaived findings in pip-audit, gitleaks,
  trivy or hadolint.
- `0.1.0` on PyPI and GHCR.

---

## 4. Dependency map

```
P0 Spikes ──▶ P1 Foundation/core ──▶ P2 Store ─────────┐
   │                │                                  ▼
   │                └──▶ P3-01…05 (pure) ──▶ P3 Ingest (writer/engine/http/grpc)
   │                                                   │
   │                                                   ▼
   │                                              P4 Query ──▶ P5 Runtime/CLI
   │                                                   │            │
   └─(S1 decisions)───────────────────────────────────▶ P6 UI ◀────┘
                                                        │
                                                        ▼
                                              P7 Packaging/Docker ──▶ M2 0.1.0a1
                                                        │
          P3 done ──▶ P8 Recipes/dataset ──────────────▶│ (P7-03 needs P8-03)
                      P8-05/06 Benchmarks ◀── P7-01     │
                                                        ▼
          P9 Docs/site (P9-05 blocked on Q-T1) ──▶ P10 Hardening ──▶ M3 0.1.0
```

**Critical path:** P0 → P1 → P2 → P3-06…P3-13 → P4 → P6 → P7 → P8-05/06 → P10
(≈ 11.5 + 8.5 + 16 + 11.5 + 9 + 21.5 + 7 + 4 + 5.5 ≈ 95 d of serial work, up to ≈ 105 d depending on how much of P8-01…04
and P9 runs in parallel with P6).

---

## 5. Implementation risks

| Risk | Probability | Impact | Mitigation | Owner |
|---|---|---|---|---|
| S1 fails: the wheel cannot boot Reflex without a JS runtime, or internal `__REFLEX_*` env vars change | Medium | High (REQ-200.b, 210) | P0 runs first; Reflex pinned `>=0.10.1,<0.11`; wheel-boot CI job; on failure, stop and open a Delta Spec before P6 | Ernesto |
| S2 misses REQ-305 on SQLite | Medium | Medium | Rollup ladder step 1 decided in P0-10 before the schema is frozen; DuckDB only via Delta Spec | Ernesto |
| GIL contention between normalizer and UI under load | Medium | Medium | P8-06 measures; `ProcessPoolExecutor` escape hatch (DD-001) behind the same API | Ernesto |
| GenAI semconv moves (genai repo release, renames) during the build | High | Medium | Pinned commit + `MAPPING_VERSION`; mapping change + `rebuild`; re-pin to a release tag if one is cut before 0.1.0 (Q-T2) | Ernesto |
| Third-party instrumentations change attributes between fixture recording and release | Medium | Medium | Fixtures record versions (REQ-260.b); re-record in P10-04 if a minor bumps | Ernesto |
| silverpoint / mermaidjs gaps or bugs block a chart or graph interaction | Medium | Low/Medium | Closest available chart (Art. 7); same author controls upstream; issues filed in P6-13 | Ernesto |
| Solo-maintainer bandwidth; long plan (≈ 20 weeks) | High | Medium | Agent-delegable tasks marked; "3–5 tasks per run, review, then scale"; milestone M2 gives an early usable alpha | Ernesto |
| Q-T1 stays open | Medium | Low | Only P9-05 is blocked; everything else proceeds; site image is built in CI regardless | Ernesto |
| No reference machine available | Low | Medium | Any matching cloud VM (4 vCPU, 8 GiB, NVMe) is acceptable; record its spec in the report | Ernesto |
| Hidden content leak through a third-party library's logs/exceptions | Low | High | Content guard filter at the root logger; canary suites in P3 and P10 | Ernesto |

---

## 6. Communication and tracking

The project has one maintainer, so ceremonies are replaced by artifacts in the repo:

| Practice | Frequency | Purpose |
|---|---|---|
| Status column in this plan and in `specs/tasks/agentlens-tasks.md` | At every task merge | Any session (human or agent) can resume from the files |
| Agent runs of 3–5 tasks, then review | Per run | Skill rule "first run 3–5 tasks, review, adjust, then scale" |
| Phase review: Done criteria checked, P1-05 coverage report attached to the merge PR | End of each phase | Gate before the next phase |
| `CHANGELOG.md` via Conventional Commits | Per PR | Release notes are mechanical (Art. 10) |
| Spike and benchmark reports in Tech Design §11 / CI artifacts | P0, P8-06, each release tag | Decisions recorded where they are used |

Branching (Art. 10): one branch per task or small task group from `develop` (`feat/P3-04-redaction`,
…), PR into `develop` with CI green; releases merged into `main` and tagged.

---

## 7. Definition of Done (global)

- [ ] Code merged into `develop` via PR with all CI jobs green
- [ ] Tests written first or in the same commit for `ingest`/`store`/`query`; each test cites its `REQ-NNN.x`
- [ ] Coverage ≥ 85 % on `ingest`+`store`+`query`; `mypy --strict` and import contracts clean
- [ ] Contract suite green on SQLite, PG 15 and PG 18 for any `store` change
- [ ] No content or keys in logs, errors or self-telemetry (Art. 3)
- [ ] Affected spec updated in the same PR, or a Delta Spec opened under `changes/` (Art. 10)
- [ ] Docs updated when a user-visible behavior, setting or CLI flag changes
- [ ] No known technical debt without a GitHub issue

---

## 8. Open items carried by this plan

| Item | Resolved by |
|---|---|
| Amendment A6 (fixed standalone port) | P0-06 |
| DD-018 CSP fallback | P0-06 |
| Rollup tables (Data Model §2.19) | P0-10 |
| Q-T2 `SEMCONV_GENAI_COMMIT` | P3-01 |
| Q-T4 MkDocs maintenance re-check | P9-01 |
| Q-T1 site hosting | Maintainer; blocks P9-05 only |
| 0.1.0 target date | Maintainer, after M0 |

---

## Constitution check

| Article | How this plan complies |
|---|---|
| Art. 1 | P3-01 implements the pinned target model and one declarative mapping table; raw attributes kept (P3-02); no framework code paths. Recipes are docs + fixtures (P8-03, P9-02), not client code |
| Art. 2 | SQLite default and PG backend both under one contract suite (P2-04, P2-11); migrations only via Alembic (P2-02); no Redis/broker; rollups stay SQL (P0-10) |
| Art. 3 | Redaction and content drop before the writer (P3-04 → P3-06); content guard logging (P1-08); bundled pricing, no fetch (P3-05); zero third-party assets verified (P0-05, P6-12, P10-01) |
| Art. 4 | Key auth, salted hashes, limits, backpressure before the normalizer (P3-08…P3-10); CSP (P6-11); non-root image (P7-02); secrets from env/files (P1-07); gitleaks + pip-audit on every PR (P1-03) |
| Art. 5 | `Decimal` / nano-USD `BIGINT` (P1-06); unpriced as NULL + flag, pricing version per cost (P3-05); idempotent ingest and order invariance proved by property tests (P3-12) |
| Art. 6 | Import contracts and `mypy --strict` from P1-02; layers built bottom-up; aggregates in SQL with bounded-rows tests (P2-07, P4-03) |
| Art. 7 | Pages built as embeddable callables first (P6-03), standalone only as a shell over them (P6-11); silverpoint for charts, mermaidjs for the graph; waterfall as a table per the recorded interpretation; upstream issues filed (P6-13) |
| Art. 8 | Test-first rule in the global DoD; property (P3-12), golden (P3-12, P8-04), contract (P2-11), smoke (P6-12), Docker (P7-03/04) and security (P10-01) suites; traceability script (P1-05 → blocking in P10-02); benchmarks reproducible (P8-05) |
| Art. 9 | `/healthz` and `/readyz` (P5-01); JSON logs (P1-08); self-telemetry off by default with loop check (P5-07) |
| Art. 10 | Spikes before dependent work; Tasks and Analyze before implementation; branches from `develop`; Conventional Commits and changelog (P7-05); spec updates in the DoD; Analyze re-run before release (P10-03); all artifacts in English |

**Exceptions requested:** none.

---

## Change history

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0-draft.1 | 2026-10-31 | Ernesto Crespo (with Claude) | Initial draft from Constitution v1.1, PRD v1.1, API v1.1, Tech Design v1.1, Data Model v1.0 |
| 1.0 | 2026-10-31 | Ernesto Crespo | Approved |
