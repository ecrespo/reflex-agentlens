# Tasks — reflex-agentlens v0.1

> Source specs: [Constitution](../constitution.md) v1.2 · [PRD](../prd/agentlens-v0.1.md) v1.1 ·
> [API Spec](../api/agentlens-api-v1.md) v1.3 · [Tech Design](../technical/agentlens-architecture.md) v1.4 ·
> [Data Model](../data-model/agentlens-schema.md) v1.2 · [Implementation Plan](../plans/agentlens-phase-1.md) v1.0
> Plan phases detailed in this revision: **P0 (spikes), P1 (foundation and `core`), P2 (store) and P3 (ingest)** · Generated: 2026-10-31
> Status: `APPROVED` (P3 detailing adopted under the standing authorization of 2026-10-31) · Version 1.2

## How this file works

- **Rolling wave.** The plan has ~95 tasks across 11 phases. Detailing all of them now would be
  wasted work: the S1 and S2 spikes (P0) can change the store schema (rollups), the serve wiring and
  the CSP. Therefore only P0 and P1 are broken down into atomic tasks here. Each later phase is
  detailed **at its start**, in this same file, as a new revision that goes through review. The
  traceability matrix at the end already covers **every** PRD requirement at plan-task level, so the
  Analyze gate can check coverage for the whole release now.
- **IDs.** Atomic tasks are `P<phase>-<nn>.<k>` and belong to plan task `P<phase>-<nn>`.
- **Order** = execution order unless marked **[P]** (parallelizable: no dependency and no shared files).
- **States:** `[ ]` pending · `[~]` in progress · `[x] YYYY-MM-DD` done · `[!]` blocked (note why).
- **Branches** (Constitution Art. 10): cut from `develop`. Spike work lives on `spike/s1-prebuilt`
  and `spike/s2-sql` and is **never merged** into `develop`. Only the spike *reports* (spec edits) and
  P0-01 are merged. Every other task: `feat/<task-id>-<slug>` → PR into `develop`.
- **Tests** cite the criterion they verify (`REQ-NNN.x`) in the test name or docstring (Art. 8). In
  `core`, `ingest`, `store` and `query` the failing test comes first or in the same commit.
- **Agent execution:** batches of 3–5 tasks, then human review, then the next batch. Record each
  batch in the execution log at the end of this file.
- If implementation shows a spec is wrong: **stop, update the spec or open a Delta Spec under
  `changes/`, then continue.** Code and specs never diverge silently.

---

## Phase P0 — Spikes S1 and S2

Spike numbers are measured on the maintainer's machine and labeled with its spec. They inform the
decisions; they do not replace the reference-machine benchmark (P8-06).

### P0-01.1 · Minimal project scaffold
- **What**: `pyproject.toml` for `reflex-agentlens` (hatchling, `src/` layout, `requires-python = ">=3.11"`,
  MIT, `[project.scripts] reflex-agentlens = "reflex_agentlens.cli.main:main"` pointing at a stub that
  prints the version), `src/reflex_agentlens/__init__.py` with `__version__ = "0.1.0.dev0"`, a `uv`
  workspace root with no members yet, `uv.lock`, ruff config (line length, target `py311`), a `dev`
  dependency group with `pytest` and `ruff`, and one smoke test.
- **REQ / design**: DD-022 (base for P1-01)
- **Files**: `pyproject.toml`, `uv.lock`, `src/reflex_agentlens/__init__.py`, `src/reflex_agentlens/cli/{__init__,main}.py`, `tests/unit/test_package.py`
- **Depends on**: —
- **Done**: `uv sync && uv run pytest && uv run ruff check && uv run ruff format --check && uv build`
  all succeed; `uv run reflex-agentlens` prints `0.1.0.dev0`. Merged into `develop`.
- [x] 2026-10-31 · PR #2

### P0-02.1 · S1a — build-only web app with the real UI dependencies
- **What**: On `spike/s1-prebuilt`, add `reflex>=0.10.1,<0.11`, `reflex-silverpoint-react>=0.8.1,<0.9`,
  `reflex-mermaidjs>=0.2.1,<0.3`. Create `src/reflex_agentlens/_webapp/` with one page that renders a
  silverpoint `bar_chart`, a Mermaid `flowchart LR` (`security_level="strict"`, `font_awesome=False`,
  `icon_packs=[]`) and a list of Pydantic v2 DTOs held in a state var. Run
  `reflex export --frontend-only --no-zip` with Bun 1.4.4.
- **REQ / design**: DD-002, DD-011, DD-017, Tech Design §11 S1 ("Pydantic DTOs as state vars")
- **Depends on**: P0-01.1
- **Done**: `.web/build/client/index.html` exists; the page renders all three elements under
  `reflex run` (dev). Note the bundle size and any export warnings in the spike notes.
- [x] 2026-10-31 · spike/s1-prebuilt c2523bd

### P0-02.2 · S1a — prototype hatchling build hook
- **What**: `hatch_build.py` custom build hook that runs the export from P0-02.1 and packages
  `.web/build/client/**` as `reflex_agentlens/_frontend/` inside the wheel.
- **REQ / design**: DD-002 (prototype of P7-01)
- **Depends on**: P0-02.1
- **Done**: `uv build` then `unzip -l dist/*.whl | grep _frontend/index.html` prints a match.
- [x] 2026-10-31 · spike/s1-prebuilt b51ef67

### P0-03.1 · S1b — boot the installed wheel with no JS runtime
- **What**: `spikes/s1/boot.py` (spike branch) implementing the DD-002 runtime recipe: copy the bundle
  to `{data_dir}/web/<version>/`, write a minimal `rxconfig.py`, set `REFLEX_WEB_WORKDIR`,
  `REFLEX_STATES_WORKDIR`, `REFLEX_ENV_MODE=prod`, `__REFLEX_MOUNT_FRONTEND_COMPILED_APP=true`,
  `__REFLEX_SKIP_COMPILE=true`, and serve the app through Granian's ASGI factory with one worker.
  Run it from a fresh venv where only the wheel is installed and `PATH` contains no `node`/`bun`.
- **REQ / design**: REQ-200.b, DD-002, Tech Design §11 S1
- **Depends on**: P0-02.2
- **Done**: in that venv, `command -v node bun` finds nothing; `GET /` → 200; a Playwright page load
  shows the chart, the graph and the DTO list rendered from state (websocket `/_event` working).
  Record time from process start to first 200 (input for REQ-210.b).
- [x] 2026-10-31 · spike/s1-prebuilt eb69011

### P0-03.2 · S1b — baked API URL / port investigation
- **What**: Find where the compiled frontend gets its backend URL (`env.json` fetched at runtime vs.
  inlined into the JS bundle). Try serving on port 9000 with the URL rewritten in the copied workdir,
  and behind a reverse proxy path.
- **REQ / design**: DD-002 consequence 1, Amendment A6
- **Depends on**: P0-03.1
- **Done**: a written finding with evidence (file path and the request that loads it). Either the
  app works on port 9000 after the rewrite (A6 not needed), or the failure mode is documented and A6
  is proposed.
- [x] 2026-10-31 · spike/s1-prebuilt eb69011

### P0-04.1 · S1c — ingest thread with grpc.aio next to Granian
- **What**: In the spike boot script, start a dedicated thread running its own asyncio loop with a
  `grpc.aio` server on `:4317` implementing a counting `TraceService/Export`. Mount a Starlette
  `POST /v1/traces` route on the Reflex backend (via `api_transformer`) that hands raw bytes to the
  ingest loop with `asyncio.run_coroutine_threadsafe` and awaits the wrapped future.
- **REQ / design**: REQ-004.a, DD-001
- **Depends on**: P0-03.1
- **Done**: an `opentelemetry-sdk` client with the OTLP HTTP **and** gRPC exporters sends 10k spans;
  both receivers count them all. While exporting, the UI page stays responsive: event round-trip p95
  measured with Playwright is recorded (no target; input for DD-001's GIL concern).
- [x] 2026-10-31 · spike/s1-prebuilt e12432b

### P0-04.2 · S1c — shutdown ordering
- **What**: Handle SIGTERM: close the HTTP receiver, drain the ingest loop, stop the gRPC server,
  join the thread, exit.
- **REQ / design**: REQ-331.a (feasibility only), DD-001
- **Depends on**: P0-04.1
- **Done**: `kill -TERM` during an export burst → process exits 0 within 5 s; no thread or port left
  open (`ss -ltn` shows `:4317` and the HTTP port released).
- [x] 2026-10-31 · spike/s1-prebuilt e12432b

### P0-05.1 · S1d — CSP with build-time hashes
- **What**: Compute `sha256` hashes of every inline `<script>` in the exported `index.html`. Serve
  the page with `Content-Security-Policy: default-src 'self'; script-src 'self' <hashes>; style-src
  'self' 'unsafe-inline'; connect-src 'self' ws://<host>; img-src 'self' data:; font-src 'self'`.
  Check whether inline scripts differ between two builds of the same source and between requests.
- **REQ / design**: REQ-193.a, DD-018, Amendment A5
- **Depends on**: P0-03.1
- **Done**: Playwright run with the header enforced shows no CSP violation in the console, no
  `unsafe-eval`, and the page fully interactive. Finding on hash stability recorded (decides the
  DD-018 fallback).
- [x] 2026-10-31 · spike/s1-prebuilt e12432b

### P0-05.2 · S1d — zero third-party requests
- **What**: Playwright request interception over a full page session (load, chart hover, graph
  render, state updates) logging every request whose origin is not the app's origin.
- **REQ / design**: REQ-324.b, DD-011, DD-017
- **Depends on**: P0-03.1
- **Done**: zero foreign-origin requests and zero frontend console errors.
- [x] 2026-10-31 · spike/s1-prebuilt e12432b

### P0-05.3 · S1d — import-time side effects of Reflex (Analyze A-16)
- **What**: In a fresh interpreter with socket creation and filesystem writes blocked (audit hook via
  `sys.addaudithook` on `socket.*` and `open` in write modes, `os.mkdir`), import `reflex`,
  `reflex_silverpoint_react`, `reflex_mermaidjs` and the spike page module. Record every blocked
  operation and its origin.
- **REQ / design**: REQ-190.c
- **Depends on**: P0-02.1
- **Done**: either zero blocked operations, or the list of Reflex-originated ones with a proposal for
  how the REQ-190.c test scopes them (to be decided in P0-06.1).
- [x] 2026-10-31 · spike/s1-prebuilt e12432b

### P0-06.1 · S1 report and decisions
- **What**: Append "S1 results" to Tech Design §11 (machine spec, versions, timings, findings) and
  record: the DD-002 consequence 1 decision (rewrite vs. fixed port), the DD-018 fallback decision,
  whether Pydantic DTOs work as state vars, and A6 applied (PRD v1.2, §7.4 known limitation) or
  dropped. Bump Tech Design to v1.2.
- **REQ / design**: DD-002, DD-018, A6, Art. 10
- **Depends on**: P0-03.2, P0-04.2, P0-05.1, P0-05.2, P0-05.3
- **Done**: maintainer approves the spec edits; PR with spec-only changes merged into `develop`.
  If S1 failed outright: a Delta Spec is opened under `changes/` and P6/P7 stay blocked.
- [ ] 2026-10-31 · spec edits in PR `docs/spike-results` (Tech Design v1.2), awaiting maintainer approval

### P0-07.1 · S2a — prototype hot tables on both backends  [P]
- **What**: On `spike/s2-sql`, SQLAlchemy Core definitions for `sources`, `sessions`, `runs`,
  `spans`, `llm_calls`, `tool_calls`, `graph_nodes`, `span_search` exactly as Data Model §2.6–2.15
  (types per §2.0, indexes included), created on SQLite (WAL) and PostgreSQL 18 (container).
- **REQ / design**: Data Model §2, DD-008
- **Depends on**: P0-01.1
- **Done**: `create_all` succeeds on both; `PRAGMA compile_options` shows FTS5 and the SQLite version
  is ≥ 3.43; PG version logged.
- [x] 2026-10-31 · spike/s2-sql 6b1d93e

### P0-07.2 · S2a — synthetic dataset generator
- **What**: Seeded generator producing Runs of ~20 spans (agent root, LLM calls with tokens and cost,
  tool calls with some errors, graph nodes, Sessions of 1–10 Runs, 10 models, 30 tools, 5 Sources)
  at 100k and 1M spans, spread over 30 days.
- **REQ / design**: PRD §7.1 dataset definition, REQ-366 (prototype of P8-05)
- **Depends on**: P0-07.1
- **Done**: same seed → identical row counts and identical checksum of generated IDs; 1M-span
  load completes on both backends.
- [x] 2026-10-31 · spike/s2-sql 86e890c

### P0-08.1 · S2b — write-path throughput
- **What**: In-process write path: 4 producers submit 512-span batches into a queue; one writer
  groups (≤ 50 ms or ≤ 5,000 spans) into one transaction doing Q6 (insert-or-ignore with
  `RETURNING`) and Q7 (set-based Run/Session recompute for affected traces). Run 2 minutes per backend.
- **REQ / design**: REQ-300.a, REQ-301.a (indicative), DD-003, DD-004, Data Model Q6/Q7
- **Depends on**: P0-07.2
- **Done**: JSON result with spans/s, commit latency p50/p95, and group sizes per backend.
- [x] 2026-10-31 · spike/s2-sql 86e890c

### P0-08.2 · S2b — order-invariance sanity check
- **What**: Ingest the same 10k-span set in generation order, reversed, and shuffled into random
  batch sizes, each into a fresh database.
- **REQ / design**: REQ-037.a (early check of DD-004)
- **Depends on**: P0-08.1
- **Done**: the three `runs` and `sessions` tables are identical (row-level hash match).
- [x] 2026-10-31 · spike/s2-sql 86e890c

### P0-09.1 · S2c — Runs and Trace query latency
- **What**: Q1 (default page), Q2 (one filter each: model, tool, status, duration range), Q3
  (attribute filter and a two-term FTS query) on 100k; Q5 on a 1,000-span trace. 50 runs each,
  warm cache, p95 per query per backend.
- **REQ / design**: REQ-303.a, REQ-304.a, Data Model Q1–Q5
- **Depends on**: P0-07.2
- **Done**: JSON results; each query's `EXPLAIN` (SQLite `EXPLAIN QUERY PLAN`, PG `EXPLAIN ANALYZE`)
  saved next to the numbers.
- [x] 2026-10-31 · spike/s2-sql 86e890c

### P0-09.3 · S2c — full-text parity on identifiers (Analyze A-15)
- **What**: Index span documents containing identifiers (`search_web`, `db.query`, `gpt-4o-mini`,
  `RefundTool`) in FTS5 (`unicode61 remove_diacritics 2`) and PG (`to_tsvector('simple', …)`), and
  query each term through the DD-006 query builder rules on both backends. Record how each backend
  tokenizes `_`, `.` and `-` in documents and queries.
- **REQ / design**: REQ-073.b, DD-006
- **Depends on**: P0-07.1
- **Done**: a table of term → matching span IDs per backend. Either the sets are identical, or a
  tokenization rule that makes them identical is proposed in the S2 report (P0-10.1) for DD-006.
- [x] 2026-10-31 · spike/s2-sql 86e890c (fix proposed and applied to DD-006)

### P0-09.2 · S2c — Dashboard aggregate latency and backend parity
- **What**: Q8 (KPIs) and every Q10 aggregate over a 30-day range on 1M spans, with
  `percentile_sql()` (nearest-rank: window `ROW_NUMBER()` on SQLite, `percentile_disc` on PG — Data Model Q8).
- **REQ / design**: REQ-305.a, REQ-106, DD-005
- **Depends on**: P0-07.2
- **Done**: p95 of the full aggregate set per backend recorded; both backends return identical
  aggregate values on the same dataset (difference = none).
- [ ] 2026-10-31 · measured on spike/s2-sql 86e890c: PG 724 ms ✓, SQLite 3,327 ms ✗ (values identical) → decision in P0-10.1

### P0-10.1 · S2 report and rollup decision
- **What**: Append "S2 results" to Tech Design §11 with the numbers and the machine spec. Decide
  whether `rollup_hourly` (Data Model §2.19, ladder step 1) is adopted. If adopted, move it from
  "conditional" into `0001_initial` (Data Model v1.1).
- **REQ / design**: DD-005, Data Model §2.19
- **Depends on**: P0-08.1, P0-08.2, P0-09.1, P0-09.2, P0-09.3
- **Done**: maintainer approves the spec edits; PR merged. If REQ-305 is missed even with rollups,
  a Delta Spec (ladder step 2) is opened before P2 starts.
- [ ] 2026-10-31 · S2 results in Tech Design §11.2 (same PR); **rollup / REQ-305 decision pending maintainer**

---

## Phase P1 — Foundation and `core`

### P1-01.1 · Package skeleton and workspace
- **What**: Create the package tree of Tech Design §5.1 (`core/`, `core/semconv/`, `ingest/`,
  `ingest/normalize/`, `ingest/redaction/`, `store/`, `store/sqlite/`, `store/postgres/`,
  `store/migrations/`, `query/`, `components/`, `pages/`, `app/`, `cli/`, `data/`, `_webapp/`) with
  `__init__.py` and a one-line module docstring each; uv workspace members `site/` (`agentlens-site`)
  and `examples/` (`agentlens-examples`), each a minimal unpublished `pyproject.toml`; runtime
  dependencies with the constraints of Tech Design §9.3 (Reflex and UI deps included; versions
  adjusted only if P0 findings require it); dependency groups `dev`, `docs`, `bench`; `.gitignore`
  entries for `src/reflex_agentlens/_frontend/`, `.web/`, `.states/`, local data dirs.
- **REQ / design**: DD-022, Constitution stack constraints
- **Depends on**: P0-01.1, P0-06.1 (dependency pins confirmed)
- **Done**: `uv sync --all-packages` and `uv lock --check` succeed; `uv run python -c "import reflex_agentlens"`
  succeeds; licenses of all runtime deps are in the Constitution allowlist (checked with
  `uvx pip-licenses` or equivalent, output attached to the PR).
- [~] 2026-10-31 · PR #6 (all Done checks pass; MPL-2.0 transitive deps `bidict`, `certifi` via Reflex await a Constitution licensing decision)

### P1-02.1 · Ruff and mypy configuration
- **What**: Ruff lint rule set (incl. `S` bandit rules, `T20` no print outside `cli`), `ruff format`;
  mypy `strict = true` for `reflex_agentlens.core`, `.ingest`, `.store`, `.query`.
- **REQ / design**: REQ-360.a, Art. 6
- **Depends on**: P1-01.1
- **Done**: `uv run ruff check`, `uv run ruff format --check` and `uv run mypy` pass with zero findings.
- [x] 2026-10-31 · PR #7

### P1-02.2 · Import contracts
- **What**: `import-linter` contracts in `pyproject.toml`: the layers contract and every forbidden
  contract of Tech Design §5.1 (core layers ↛ `reflex`, `reflex_silverpoint_react`, `reflex_mermaidjs`;
  `ingest` ↛ `query`; `store` ↛ `ingest`; `query` ↛ `ingest` except `ingest.redaction.preview`;
  `pages`/`components` ↛ `store`).
- **REQ / design**: REQ-360.a, Art. 6, API §1.1
- **Depends on**: P1-01.1
- **Done**: `uv run lint-imports` passes; a test that adds a temporary forbidden import (e.g.
  `import reflex` in `store`) makes it fail (documented in the PR).
- [x] 2026-10-31 · PR #7 (temporary `import reflex` in `store` breaks the contract)

### P1-02.3 · Pytest and coverage configuration
- **What**: pytest config (`asyncio_mode = "auto"`, markers `pg`, `slow`, `smoke`, `docker`, test dirs
  of Tech Design §5.1), `hypothesis` profiles (`ci`, `dev`), coverage over `ingest`+`store`+`query`
  with `fail_under = 85`, `pg` tests skipped when `AGENTLENS_TEST_PG_URL` is unset.
- **REQ / design**: REQ-361.a
- **Depends on**: P1-01.1
- **Done**: `uv run pytest --cov` runs; the coverage gate is wired (verified by temporarily adding an
  untested function under `store/` and seeing the gate fail).
- [x] 2026-10-31 · PR #7 (temporary untested module under `store` fails the gate)

### P1-03.1 · CI: lint, types, tests
- **What**: `.github/workflows/ci.yml` on PRs to `develop`/`main`: jobs `lint` (ruff + import-linter),
  `types` (mypy), `test` (Python 3.11/3.12/3.13 × SQLite), `test-pg` (3.13 × PostgreSQL 15 and 18
  service containers with `AGENTLENS_TEST_PG_URL`). `astral-sh/setup-uv`; every action pinned by
  commit SHA; `uv sync --frozen`.
- **REQ / design**: REQ-361.a, REQ-360.a
- **Depends on**: P1-02.1, P1-02.2, P1-02.3
- **Done**: a PR into `develop` shows all jobs green; a deliberate ruff violation in a throwaway
  branch makes `lint` fail.
- [~] 2026-10-31 · PR #8 (workflow added; first green run and the deliberate-violation check happen once the stack reaches `develop`)

### P1-03.2 · CI: security  [P]
- **What**: `.github/workflows/security.yml` on every PR: gitleaks (full history on push, diff on PR),
  `uvx pip-audit` against the locked dependencies, both failing on new findings.
- **REQ / design**: REQ-328.a, Art. 4
- **Depends on**: P1-01.1
- **Done**: both jobs green on the PR; a fake `al_`/`AKIA…` test secret in a throwaway branch makes
  gitleaks fail.
- [~] 2026-10-31 · PR #8 (gitleaks and pip-audit green on the PR; fake-secret check pending)

### P1-04.1 · Repository guidance files  [P]
- **What**: `CLAUDE.md` and `AGENTS.md` at the repo root (short; point at `specs/constitution.md`,
  the spec-anchored process, `uv` commands, branch rules, "tests cite REQ IDs"); `CONTRIBUTING.md`
  (setup with `uv`, branching from `develop`, Conventional Commits, Delta Specs, running the PG
  tests); `CHANGELOG.md` skeleton (Keep a Changelog, `Unreleased`).
- **REQ / design**: REQ-380.a (partial), Art. 10
- **Depends on**: P1-01.1
- **Done**: files present and reviewed; links resolve.
- [x] 2026-10-31 · PR #8

### P1-05.1 · Traceability script
- **What**: `scripts/check_req_coverage.py`: parse every `REQ-NNN.x` criterion of a MUST requirement
  from the PRD (criteria explicitly marked SHOULD/COULD excluded), search `tests/` for citations,
  read exemptions with reasons from `tests/req_exemptions.toml`, print uncited criteria, exit 1 if any
  (with `--report-only` to always exit 0). Unit tests for the parser on a small PRD fixture. CI step
  in `ci.yml` with `--report-only` until P10-02.
- **REQ / design**: REQ-362.a
- **Depends on**: P1-02.3
- **Done**: `uv run python scripts/check_req_coverage.py --report-only` lists every MUST criterion as
  uncited (expected at this point) and exits 0; its own tests pass.
- [x] 2026-10-31 · PR #8 (234 MUST criteria; 232 uncited, as expected)

### P1-06.1 · `core.errors`, `core.ids`, `core.timeutil`
- **What**: The exception hierarchy of API §6.6; ID helpers (trace/span ID hex ↔ bytes, validation
  of all-zero and wrong-length IDs, ULID-style Project IDs per Data Model §1.3); UTC and Unix-ns helpers.
- **REQ / design**: API §6.6, REQ-010.a (ID validation rules), Data Model §1.3, Art. 5
- **Depends on**: P1-02.1
- **Done**: `uv run pytest tests/unit/core` green, tests citing REQ-010.a for ID validation; mypy strict clean.
- [x] 2026-10-31 · PR #9

### P1-06.2 · `core.money`  [P]
- **What**: `Decimal` ↔ nano-USD `int` conversion (exact, with rounding rule documented), per-1M-token
  price arithmetic helpers, refusal of `float` inputs.
- **REQ / design**: Art. 5 (and its recorded interpretation), REQ-061.a, Data Model §2.0
- **Depends on**: P1-02.1
- **Done**: unit tests including round-trip property test with `hypothesis` and a `float` rejection test.
- [x] 2026-10-31 · PR #9

### P1-06.3 · `core.security`  [P]
- **What**: API key generation (`al_<key_id>_<secret>`, ≥ 128 bits from `secrets`, format of API §3.4),
  salted SHA-256 hashing with 16-byte salt, constant-time verification, HMAC sign/verify helpers for
  cookies and cursors.
- **REQ / design**: REQ-320.a/b, DD-014, API §3.4
- **Depends on**: P1-02.1
- **Done**: tests citing REQ-320.b (prefix, entropy length, CSPRNG source) and REQ-320.a (hash never
  equals or contains the plaintext; wrong key fails; tampered signature fails).
- [x] 2026-10-31 · PR #9

### P1-07.1 · `core.config` models
- **What**: pydantic-settings models for every section and key of API §8.2 (including A4
  `server.public_url` and `server.ingest_enabled`), with defaults and validation, and secret fields
  typed as secrets.
- **REQ / design**: REQ-202, API §8.2, DD-019
- **Depends on**: P1-06.1
- **Done**: tests: defaults equal API §8.2; each invalid value raises an error naming the key.
- [x] 2026-10-31 · PR #9

### P1-07.2 · Configuration sources and unknown keys
- **What**: Source chain defaults → TOML (`--config` / `AGENTLENS_CONFIG`, `tomllib`) → env
  (`AGENTLENS_<SECTION>_<KEY>`) → CLI overrides; `*_FILE` resolution for secret keys; unknown TOML
  keys and unknown `AGENTLENS_*` env names reported with their source. No I/O at import.
- **REQ / design**: REQ-202.a/b/c, DD-019
- **Depends on**: P1-07.1
- **Done**: tests citing REQ-202.a (precedence), REQ-202.b (unknown key in TOML and in env, error names
  key + source), REQ-202.c (`*_FILE` for each secret).
- [x] 2026-10-31 · PR #9

### P1-07.3 · Effective config rendering with secrets masked
- **What**: Function that renders the effective configuration (TOML-like text) with every secret
  masked, used later by `--print-config`.
- **REQ / design**: REQ-202.d
- **Depends on**: P1-07.2
- **Done**: test citing REQ-202.d: secrets set via value and via `*_FILE` never appear in the output.
- [x] 2026-10-31 · PR #9

### P1-08.1 · `core.logging`
- **What**: JSON formatter (ts, level, logger, event, allowlisted extras), content guard filter with
  the allowlist of Tech Design §7.1, exception message → class name for exceptions from
  `ingest.normalize` / `ingest.redaction`, tracebacks only at `DEBUG` and without locals, level from config.
- **REQ / design**: REQ-341.a, REQ-323.a, DD-020
- **Depends on**: P1-06.1
- **Done**: tests citing REQ-341.a (valid JSON per line, required fields) and REQ-323.a (a canary
  string passed as a non-allowlisted extra and inside a normalizer exception message never appears in
  captured output).
- [x] 2026-10-31 · PR #9

---

## Phase P2 — Store

Detailed 2026-10-31 after P1 and M0. Inputs: Data Model v1.1, API v1.2, Tech Design v1.3 (DD-004
concurrency rule, Analyze A-01), REQ-305 split (Delta Spec `changes/2026-10-req-305-percentile-budget/`).
Every task lives under `src/reflex_agentlens/store/` and its tests under `tests/unit/store/` or
`tests/contract/`; failing test first or in the same commit (Art. 8). Branches:
`feat/P2-<nn>-<slug>` from `develop`.

**Design note — protocol return types.** `store` cannot import `query` (import contract). The
return types named in API §5 (`RunSummary`, `TraceDetail`, `Kpis`, …) are therefore
`store.records` frozen dataclasses with the same names and storage-level fields (IDs as bytes or
ULID strings, money as `int` nano-USD, times as `int` ns). `query.dto` builds the Pydantic DTOs of
API §6 from them with the Data Model §8 conversions. This also closes Analyze A-05 for the
storage side; the Pydantic DTO fields are fixed in P4.

**Protocol method ownership (Analyze A-06).** Each `StorageBackend` method is implemented and
contract-tested in exactly one task:

| Task | Methods |
|---|---|
| P2-05.1 / P2-06.1 | `open`, `close`, `ping` (per backend) |
| P2-02.2 | `schema_status`, `migrate` |
| P2-05.2 / P2-06.1 | `write_batch` (per backend) |
| P2-07.4 | `write_metric_points`, `agg_metric_token_usage` |
| P2-08.1 | `list_projects`, `create_project`, `update_project`, `delete_project`, `create_api_key`, `list_api_keys`, `revoke_api_key`, `resolve_api_key`, `touch_api_key` |
| P2-08.2 | `save_pricing`, `list_pricing_versions`, `get_pricing`, `list_unpriced_models`, `save_redaction_rules`, `get_ingest_settings`, `list_saved_filters`, `save_filter`, `delete_filter` |
| P2-08.3 | `finalize_stale_runs`, `rebuild`, `recompute_costs` |
| P2-07.1 | `list_runs`, `find_runs_by_trace_id`, `get_trace`, `get_span_raw`, `list_llm_calls`, `list_sources`, `distinct_values` |
| P2-07.2 | `list_sessions`, `get_session` |
| P2-07.3 | `agg_kpis`, `agg_runs_over_time`, `agg_tokens_cost_by_model`, `agg_latency_percentiles`, `agg_tool_stats`, `agg_by_source`, `agg_approvals` |
| P2-09.1 | `purge_by_age`, `purge_by_size`, `storage_usage`, `last_purge`, `job_lock` |
| P2-10.1 | `change_feed` |

### P2-01.1 · Schema: hot tables and attribute codec
- **What**: `store.schema` (one SQLAlchemy Core `MetaData` for both backends, `with_variant` /
  `ddl_if`) for Data Model §2.6–2.15 (`sources`, `resources`, `sessions`, `runs`, `spans`,
  `llm_calls`, `tool_calls`, `approvals`, `graph_nodes`, `span_search` with the FTS5 / tsvector
  variants), physical types per §2.0, every index of those sections. `store.attrs`: the §1.4
  attribute JSON codec (OTLP `AnyValue` ↔ JSON, `$b64`, NaN/Infinity strings, int64 range).
- **REQ / design**: Data Model §1.3, §1.4, §2.0, §2.6–2.15, DD-008, REQ-030.b
- **Depends on**: P1-06.1
- **Done**: `metadata.create_all` succeeds on SQLite and PG (pg-marked test); a test compares the
  created index names with the Data Model list; hypothesis round-trip test for the codec citing
  REQ-030.b.
- [x] 2026-10-31 · PR #11

### P2-01.2 · Schema: settings and auxiliary tables; row records
- **What**: The remaining tables (§2.1–2.5 `app_meta`, `projects`, `api_keys`,
  `redaction_rule_sets`, `pricing_versions`/`pricing_entries`; §2.16–2.18 `metric_points`,
  `saved_filters`, `purge_log`) with their CHECKs and FKs (cascades as §3). `store.records`: frozen
  dataclasses for every row type and every protocol input/return type (see the design note).
- **REQ / design**: Data Model §2.1–2.5, §2.16–2.18, §3, API §5
- **Depends on**: P2-01.1
- **Done**: `create_all` on both backends; a test asserts that deleting a Project cascades to every
  dependent table (REQ-170 storage side); `mypy --strict` clean.
- [x] 2026-10-31 · PR #11

### P2-02.1 · Alembic environment and `0001_initial`
- **What**: `store/migrations/env.py` (async engine, `render_as_batch` on SQLite, FTS5 shadow
  tables excluded from autogenerate), packaged with the wheel; revision `0001_initial` creating all
  of §2 except §2.19, branching per dialect (Data Model §5). It inserts `app_meta.instance_id` and
  `created_at_ns`; **no pricing seed** (Analyze A-11).
- **REQ / design**: REQ-074, Data Model §5, DD-008
- **Depends on**: P2-01.2
- **Done**: `upgrade head` on empty SQLite and PG produces a schema equal to `metadata.create_all`
  (compared by reflection); `downgrade base` works in development.
- [x] 2026-10-31 · PR #11

### P2-02.2 · Schema status, guarded migration, version checks
- **What**: `schema_status()` → `CURRENT | OUTDATED | NEWER` (from `alembic_version` vs. the
  package head); `migrate()` under `pg_advisory_lock(hashtext('agentlens:migrate'))` (waiters see
  the finished schema), in-process on SQLite; startup checks SQLite ≥ 3.43 with FTS5 and PG ≥ 15,
  raising `StorageUnavailable` with the reason.
- **REQ / design**: REQ-074.a–d, Data Model §5, TD §9.2
- **Depends on**: P2-02.1
- **Done**: tests citing REQ-074 for each status (NEWER via a fake future revision), two concurrent
  `migrate()` calls on PG (one waits, both see CURRENT), a too-old SQLite version simulated.
- [x] 2026-10-31 · PR #11

### P2-03.1 · Storage protocol and dialect operations
- **What**: `store.protocol.StorageBackend` (typed `Protocol`, API §5 v1.2) and
  `store.dialect.SqlDialectOps`: upsert helpers, JSON extraction, `percentile_sql()` (nearest
  rank, one pass for several percentiles, rank in IEEE double, Data Model v1.0.1), FTS query
  builder with token whitelist (REQ-325.b) over `core` search normalization (DD-006:
  NFKD, strip marks, lowercase, `[\W_]+` → space; shared with ingest).
- **REQ / design**: REQ-070.a, REQ-325.b, DD-005, DD-006
- **Depends on**: P2-01.2
- **Done**: unit tests for the FTS builder (operators and quotes in user input never reach SQL
  syntax, citing REQ-325.b) and for `percentile_sql` against a Python nearest-rank reference.
- [x] 2026-10-31 · PR #12

### P2-04.1 · Contract-suite harness
- **What**: `tests/contract/conftest.py`: backend-parametrized fixture (`sqlite` always, `pg` when
  `AGENTLENS_TEST_PG_URL` is set; a fresh database per test module), a seeded dataset builder for
  normalized batches (ported from the S2 generator), and a helper that asserts the number of rows
  fetched per aggregate.
- **REQ / design**: REQ-070.b, REQ-106.h
- **Depends on**: P2-03.1
- **Done**: a smoke contract test runs on SQLite locally and on PG 15/18 in CI; the rows-fetched
  helper fails on a deliberately unbounded query.
- [x] 2026-10-31 · PR #12

### P2-05.1 · SQLite backend: engines and lifecycle
- **What**: `store/sqlite/backend.py`: one WRITE connection (`BEGIN IMMEDIATE`) and a READ pool,
  pragmas of Data Model §2.0 on every connection, `open`/`close`/`ping`.
- **REQ / design**: REQ-071, DD-003, DD-008
- **Depends on**: P2-02.2, P2-04.1
- **Done**: tests: WAL active, pragmas set on every pooled connection, `ping` false after the file
  disappears.
- [x] 2026-10-31 · PR #12

### P2-05.2 · SQLite `write_batch`
- **What**: API §5.1 in one transaction: Source/Session upserts, Run stubs (Q6 sorted
  `ON CONFLICT DO UPDATE … RETURNING`), span insert-or-ignore, derived rows for new spans only,
  FTS5 documents, Q7 Run recompute and Session recompute; returns `WriteResult`.
- **REQ / design**: REQ-031, REQ-032, REQ-036, REQ-037.a, REQ-038, REQ-073.a, DD-003, DD-004
- **Depends on**: P2-05.1
- **Done**: contract tests citing REQ-038 (duplicates counted, not stored twice), REQ-037.a
  (hypothesis: any order and batching of the same span set gives identical `runs`/`sessions`
  rows), REQ-032.b (status rules with an injected clock) and REQ-036.b (Session totals).
- [x] 2026-10-31 · PR #12

### P2-06.1 · PostgreSQL backend and `write_batch` with locks
- **What**: `store/postgres/backend.py` on asyncpg: lifecycle, `write_batch` with the DD-004
  concurrency rule (Runs locked in (`project_pk`, `trace_id`) order, Sessions `FOR UPDATE` by `pk`,
  one group retry on `40P01`), `span_search` tsvector + GIN.
- **REQ / design**: REQ-072.a, REQ-073.b, DD-004 (A-01)
- **Depends on**: P2-05.2
- **Done**: the P2-05.2 contract tests pass on PG 15 and 18.
- [x] 2026-10-31 · PR #12

### P2-06.2 · Multi-writer contract test
- **What**: Two backends on the same PG database write interleaved batches of the **same** traces
  (with duplicates) concurrently; plus a finalizer racing a writer.
- **REQ / design**: REQ-072.b, REQ-205.c, Analyze A-01
- **Depends on**: P2-06.1
- **Done**: test citing REQ-072.b: after both finish, `runs` and `sessions` equal a single-writer
  ingest of the same set; repeated 20× without a deadlock escaping the retry.
- [x] 2026-10-31 · PR #12

### P2-07.1 · Reads: runs and traces
- **What**: Q1 (keyset), Q2 (derived-table filters), Q3 (attribute and text filters), Q4, Q5 and
  Q5b on both backends: `list_runs`, `find_runs_by_trace_id`, `get_trace`, `get_span_raw`,
  `list_llm_calls`, `list_sources`, `distinct_values`. Every read takes `ProjectScope`.
- **REQ / design**: REQ-100, REQ-101, REQ-103, REQ-151.e (storage side), REQ-073, Data Model Q1–Q5b
- **Depends on**: P2-05.2, P2-06.1
- **Done**: contract tests citing REQ-100/101/103 (each filter, keyset stability under inserts,
  scope isolation between Projects), identical results on both backends.
- [x] 2026-10-31 · PR #12

### P2-07.2 · Reads: Sessions
- **What**: Q9 `list_sessions` and `get_session` on both backends.
- **REQ / design**: REQ-105, Data Model Q9
- **Depends on**: P2-07.1
- **Done**: contract tests citing REQ-105.a/b.
- [x] 2026-10-31 · PR #13

### P2-07.3 · Dashboard aggregates
- **What**: Q8 and Q10a–f: `agg_kpis`, `agg_runs_over_time`, `agg_tokens_cost_by_model`,
  `agg_latency_percentiles` (Q10c, its own method so the dashboard can fetch it separately,
  REQ-305.b), `agg_tool_stats`, `agg_by_source`, `agg_approvals`; automatic bucket size.
- **REQ / design**: REQ-106.a–h, REQ-305 (shape), DD-005
- **Depends on**: P2-07.1
- **Done**: contract tests citing REQ-106.a–g on a known dataset (exact expected values, identical
  on both backends, `includes_unpriced` per REQ-061.c) and REQ-106.h (rows fetched bounded).
- [x] 2026-10-31 · PR #13

### P2-07.4 · Metric points
- **What**: `write_metric_points` and Q11 `agg_metric_token_usage` (preferred
  `gen_ai.client.inference.usage.*`, legacy `gen_ai.client.token.usage`).
- **REQ / design**: REQ-005 (storage side), REQ-107, Data Model §2.16, Q11
- **Depends on**: P2-07.3
- **Done**: contract tests citing REQ-107.a.
- [x] 2026-10-31 · PR #13

### P2-08.1 · Projects and API keys
- **What**: Project CRUD (slug rules, delete cascades), API key records (hash + salt only),
  `resolve_api_key` (Q14 by `key_id`), `touch_api_key` at most once per minute, `revoke_api_key`.
- **REQ / design**: REQ-170 (storage side), REQ-320.a, REQ-006 (storage side), Data Model §2.2–2.3, Q14
- **Depends on**: P2-05.2
- **Done**: contract tests citing REQ-320.a (no plaintext secret in any column after a key is
  created and used) and `Conflict` on duplicate slugs.
- [x] 2026-10-31 · PR #13

### P2-08.2 · Pricing, redaction rules, saved filters, ingest settings
- **What**: `save_pricing` (new immutable version, `effective_from_ns`), `list_pricing_versions`,
  `get_pricing(at_ns)` (version in effect at time t), `list_unpriced_models`,
  `save_redaction_rules`, `get_ingest_settings`, saved filters CRUD.
- **REQ / design**: REQ-060.a/c, REQ-061.b (resolution data), REQ-102.a, REQ-172 (storage side)
- **Depends on**: P2-08.1
- **Done**: contract tests citing REQ-060.a (saved versions never change), REQ-060.c (lookup by
  time) and REQ-102.a.
- [x] 2026-10-31 · PR #14

### P2-08.3 · Derived-view maintenance
- **What**: `finalize_stale_runs` (Q12 with `FOR UPDATE SKIP LOCKED` on PG), `rebuild` (re-derives
  spans through an injected `SpanDeriver`, then recomputes Runs/Sessions; tests use a fake
  deriver), `recompute_costs` (new pricing version recorded on each LLM call, Run, Session). All
  follow the DD-004 lock order.
- **REQ / design**: REQ-032.b, REQ-042, REQ-062, DD-004
- **Depends on**: P2-06.2, P2-08.2
- **Done**: contract tests citing REQ-032.b (in_progress → incomplete after the window),
  REQ-042 (rebuild is idempotent) and REQ-062.a.
- [x] 2026-10-31 · PR #14

### P2-09.1 · Retention and job locks
- **What**: Q13 purge by age and by size in bounded batches (each its own transaction, runs locked
  first on PG), Session recompute after purge, `purge_log`, `storage_usage` (estimated and
  physical bytes), `last_purge`, `job_lock` (PG advisory lock; in-process lock on SQLite).
- **REQ / design**: REQ-090, REQ-091, REQ-092.b/c, REQ-173 (storage side)
- **Depends on**: P2-07.2
- **Done**: contract tests citing REQ-090.a, REQ-091.a (purges down to ≤ 90 % of the limit),
  REQ-092.b (a second `job_lock` holder gets False).
- [x] 2026-10-31 · PR #14

### P2-10.1 · Change feed: in-process
- **What**: `store.changefeed`: thread-safe broadcaster; `write_batch`, finalizer and purge publish
  one `ChangeEvent` per Project **after** commit (`run_keys` capped at 200 with `truncated`).
- **REQ / design**: REQ-140.a (storage side), DD-007, API §5.2
- **Depends on**: P2-05.2
- **Done**: tests: no event for a rolled-back transaction; events arrive after commit; cap honoured.
- [x] 2026-10-31 · PR #15

### P2-10.2 · Change feed: PostgreSQL NOTIFY/LISTEN
- **What**: `pg_notify` inside the commit transaction (payload ≤ 7,900 bytes, else `truncated`),
  one LISTEN connection per process with `origin_id` filtering (no echo of own events), polling
  fallback while the LISTEN connection is down.
- **REQ / design**: REQ-140.a, REQ-205.c, DD-007
- **Depends on**: P2-10.1, P2-06.1
- **Done**: pg-marked tests: a second process's write is seen within 3 s; own events not
  duplicated; LISTEN connection killed → polling delivers, then LISTEN resumes.
- [x] 2026-10-31 · PR #15

### P2-11.1 · Contract-suite completion and phase gate
- **What**: Cross-backend parity tests (run lists, search result sets per REQ-073.b with DD-006
  normalization, percentiles, Sessions; natural keys only); CI runs `upgrade head` from empty on
  SQLite and PG 15/18.
- **REQ / design**: REQ-070.b, REQ-072.a, REQ-073.b, REQ-074
- **Depends on**: P2-07.1 … P2-10.2
- **Done**: plan P2 Done criteria: contract suite green on SQLite, PG 15 and 18 in CI; `store`
  coverage ≥ 85 %; `mypy --strict` clean; no import of `ingest`/`query`/`reflex`.
- [x] 2026-10-31 · PR #15

**Suggested batches:** (1) P2-01.1, P2-01.2, P2-02.1, P2-02.2 · (2) P2-03.1, P2-04.1, P2-05.1,
P2-05.2 · (3) P2-06.1, P2-06.2, P2-07.1 · (4) P2-07.2, P2-07.3, P2-07.4, P2-08.1 · (5) P2-08.2,
P2-08.3, P2-09.1 · (6) P2-10.1, P2-10.2, P2-11.1.

---

## Phase P3 — Ingest

Detailed 2026-10-31 after P2. Inputs: API v1.3, Tech Design v1.4, Data Model v1.2 (Delta Spec
`changes/2026-10-p3-ingest-prep/`, closing Analyze A-04, A-07, A-08 and A-17). Code lives under
`src/reflex_agentlens/core/semconv/` and `src/reflex_agentlens/ingest/` (Tech Design §5.1); tests
under `tests/unit/core/`, `tests/unit/ingest/`, `tests/golden/`, `tests/property/` and
`tests/integration/`. `ingest` never imports `query` or `reflex` (import contracts). Failing test
first or in the same commit (Art. 8). Branches: `feat/P3-<nn>-<slug>` from `develop`.

**Design notes.**
- The normalizer is a pipeline of **pure functions** (Tech Design §5.2): `decode → validate →
  redact → truncate → content drop → derive → cost → search document`. Order per Tech Design v1.4
  Flow 1 (redaction before truncation, A-07). No I/O inside it; settings, pricing and the session
  key arrive as arguments (snapshots taken by the engine).
- Store additions owned by P3 (protocol ownership, Analyze A-06): `write_group`,
  `get_or_create_secret` (P3-06.1).

### P3-01.1 · `core.semconv` constants and pinned GenAI commit
- **What**: `constants.py`: `SEMCONV_CORE = "1.44.0"`, `SEMCONV_GENAI_COMMIT` (a full SHA of the
  GenAI conventions source used for §5.3), and every attribute key, event name, operation name and
  metric name of Tech Design §5.3/§5.4 as constants. **Closes Q-T2.**
- **REQ / design**: REQ-035, Art. 1, DD-009, Tech Design §5.3–5.4
- **Depends on**: P1-06.1
- **Done**: unit test asserts the pins (40-hex SHA) and that every key referenced by the mapping
  table is a constant; README/Tasks record the pinned commit.
- [x] 2026-10-31 · PR #17

### P3-01.2 · Declarative mapping table and dialect resolution
- **What**: `mapping.py`: `MAPPING_VERSION = 1`; one table of `Field → (Source, …)` in priority
  order with `attr`, `json(attr, path)` and `flat(prefix)` sources, exactly Tech Design §5.3 + §5.4;
  span classification rules (first match wins). `dialects.py`: resolution when several dialects are
  present (spec `gen_ai.*` wins, REQ-035.b). No framework code paths.
- **REQ / design**: REQ-035.a/b, REQ-251, DD-009, DD-010
- **Depends on**: P3-01.1
- **Done**: one table-driven test per mapping row and per classification rule; a both-dialects
  fixture resolves to the spec value; a test renders the table to Markdown (input for P9-01).
- [x] 2026-10-31 · PR #17

### P3-01.3 · Messages normalizer
- **What**: `messages.py`: attribute form (`gen_ai.input/output.messages`, system instructions),
  event form (`gen_ai.*.message`, `gen_ai.choice`, `gen_ai.client.inference.operation.details`),
  OpenInference flattened lists and legacy `gen_ai.prompt`/`completion` → one `Message` dataclass
  (`role`, parts `text` / `tool_call` / `tool_result` / `reasoning` / `other{type, raw_json}`).
  Input behind a small interface so log records can feed it later.
- **REQ / design**: REQ-034.a/b, Data Model N1, Tech Design §5.3 (messages)
- **Depends on**: P3-01.2
- **Done**: the same conversation in each of the four forms normalizes to equal `Message` lists;
  unknown part types land in `other` with their JSON.
- [x] 2026-10-31 · PR #17

### P3-02.1 · Decode and validate
- **What**: `ingest/decode.py`: `ExportTraceServiceRequest` / `ExportMetricsServiceRequest` from
  protobuf (upb) and OTLP/JSON (`json_format`, hex IDs) → `DecodeError` (400). `ingest/validate.py`:
  API §3.6 rules incl. `future_timestamp` and `older_than_retention` (A-17), rejection counter per
  reason and the `partialSuccess` message builder (fixed keys, never content).
- **REQ / design**: REQ-001.c, REQ-002.a, REQ-010.a/b, API §3.3, §3.6
- **Depends on**: P3-01.1
- **Done**: protobuf and JSON of the same request decode to equal spans; one test per rejection
  reason; the message lists counts per reason in a stable order.
- [x] 2026-10-31 · PR #18

### P3-02.2 · Span normalization: Source, attributes, truncation, classification
- **What**: `normalize/spans.py`: Source identification (`service.name` + namespace,
  `unknown_service:*` handling), resource hashing, OTLP attributes → JSON via `store.attrs`, events
  and links, value truncation **after redaction** (marker + flag, `max_attribute_value_bytes`),
  size estimate. `normalize/classify.py`: category and `is_genai` from the mapping rules.
- **REQ / design**: REQ-030.a/b, REQ-031.a/b, REQ-041, Tech Design Flow 1 step 9 (A-07)
- **Depends on**: P3-01.2, P3-02.1
- **Done**: raw attributes round-trip; truncated values carry the marker and set the flag; one
  classification test per rule; generic (non-GenAI) spans keep their raw data.
- [x] 2026-10-31 · PR #18

### P3-03.1 · LLMCall derivation and token totals
- **What**: `normalize/llm.py`: provider, request/response model, tokens (incl. cached, cache
  write, reasoning), finish reasons, params, messages via P3-01.3; DD-012 totals (`usage_source`
  `llm` / `agent` / `none`, aggregated usage ignored for totals).
- **REQ / design**: REQ-033.a, REQ-032.d, DD-012
- **Depends on**: P3-01.3, P3-02.2
- **Done**: fixtures for spec, OpenInference and legacy LLM spans; agent-span usage only counts
  when the trace has no LLM spans (DD-012 test).
- [ ]

### P3-03.2 · ToolCall and Approval derivation
- **What**: `normalize/tool.py` (name, call ID, type, args, result, error) and
  `normalize/approval.py`: `reflex_agentkit.approval.*` / `reflex_mcphost.approval.*` vocabulary,
  PydanticAI deferral → `pending` linked by tool-call ID, MCP elicitation (COULD).
- **REQ / design**: REQ-033.b/c, DD-010, Tech Design §5.4
- **Depends on**: P3-02.2
- **Done**: one fixture per vocabulary and decision value; tool errors from status and
  `error.type`; approval linked to its tool call.
- [ ]

### P3-03.3 · GraphNode, Session key, search document, pipeline
- **What**: `normalize/graph.py` (spec agent/workflow spans, OpenInference `graph.node.*`, LangGraph
  metadata); Session external ID from the mapping with the A-08 rule (if redaction changed it:
  `redacted~hex(HMAC(session_key, raw))[:16]`, raw never kept); `normalize/search.py` (search
  document ≤ 32 KiB, DD-006 normalization applied by the store); `normalize/__init__.normalize_batch`
  composing the whole pure pipeline into a `NormalizedBatch` plus rejection counts.
- **REQ / design**: REQ-036.a/c, REQ-073.a, DD-011, Data Model §2.8 (A-08)
- **Depends on**: P3-03.1, P3-03.2, P3-04.1
- **Done**: two conversation IDs that redact to the same text stay two Sessions; LangGraph and
  OpenInference graph fixtures; search document cap; `normalize_batch` is deterministic.
- [ ]

### P3-04.1 · Redaction presets and rules engine
- **What**: `ingest/redaction/presets.py` (`secrets`, `email`, `phone`, `credit_card` with Luhn),
  `rules.py` (pattern and field rules, compile once per rule-set version), `engine.py` (every string
  leaf of span, resource and event attributes and status message; JSON-string attributes redacted
  leaf by leaf; `regex` timeout → `[REDACTED:timeout]` fail closed).
- **REQ / design**: REQ-039.a–d, DD-013, Art. 3
- **Depends on**: P3-02.1
- **Done**: positive/negative fixtures per preset; Luhn; a catastrophic pattern times out and
  redacts; canary: a secret straddling the truncation limit leaves no prefix (A-07).
- [x] 2026-10-31 · PR #18

### P3-04.2 · Redaction hook, content drop, preview
- **What**: `redaction/hook.py` (`module:function`, called after the rules; raise → span rejected
  `redaction_hook_error`), content drop of the content-bearing keys (Tech Design §5.3) when the
  Project disabled content, `redaction/preview.py` (rule set + sample → redacted sample, for
  Settings; the only `ingest` module `query` may import).
- **REQ / design**: REQ-039.e/f, REQ-040.a/b, REQ-172.b
- **Depends on**: P3-04.1
- **Done**: hook error rejects only that span; content-off Project stores no content key; preview
  equals what ingest would store.
- [ ]

### P3-05.1 · Bundled pricing data
- **What**: `scripts/update_pricing.py` (dev-time: LiteLLM JSON → `data/pricing.csv`, license
  checked, `NOTICE` attribution), `ingest/cost.load_bundled_pricing()` and a bootstrap helper that
  saves it as pricing version 1 (`source="bundled"`) when no version exists.
- **REQ / design**: REQ-060.a/b, DD-016, Art. 3 (no runtime fetch), Data Model §5 (A-11)
- **Depends on**: P2-08.2
- **Done**: CSV parses into valid `PricingEntry` rows (Decimal → nano-USD, no floats); bootstrap is
  idempotent; no network access at runtime (test with sockets blocked).
- [ ]

### P3-05.2 · Cost per LLMCall
- **What**: `ingest/cost.py`: price each LLMCall with `core.pricing` against the engine's pricing
  snapshot; unpriced → `NULL` + flag; `pricing_version` recorded; cache-write price (COULD).
- **REQ / design**: REQ-061.a–c, Art. 5, DD-016
- **Depends on**: P3-03.1, P3-05.1
- **Done**: matching-order tests (response before request model, exact before glob); integer
  nano-USD arithmetic only.
- [ ]

### P3-06.1 · Store: `write_group` and `get_or_create_secret`
- **What**: protocol + both backends: `write_group(batches)` with one SAVEPOINT per batch, data
  errors → `BatchRejected`, other errors fail the call, PG deadlock retries the group once; events
  only for committed batches. `get_or_create_secret("session_key")` in `app_meta`.
- **REQ / design**: REQ-009.a, API v1.3 §5.1, Tech Design DD-003 (A-04), Data Model §2.1 (A-08)
- **Depends on**: P2-10.1
- **Done**: contract test: a group where one batch's Project is deleted mid-flight commits the
  others and rejects that one; two backends on one PG database get the same secret.
- [ ]

### P3-06.2 · `ingest.writer` and `WriteQueue`
- **What**: bounded queue by bytes (default 256 MiB), linger ≤ 50 ms / ≤ 5,000 spans, futures
  resolved after commit (`WriteResult` / `BatchRejected` / `StorageUnavailable`), commit-latency
  window (p50 over 5 s) for admission, `ping()` retry with backoff 0.5 → 5 s after an outage.
- **REQ / design**: REQ-009.a–c, REQ-330, DD-003
- **Depends on**: P3-06.1
- **Done**: no future resolves before its commit; queue full → immediate rejection; DB outage →
  `StorageUnavailable`, then automatic recovery.
- [ ]

### P3-07.1 · `ingest.engine`: thread, loop, supervisor
- **What**: dedicated thread with its own loop, `submit()` from other loops
  (`run_coroutine_threadsafe`), start/stop with drain (`shutdown_grace`), supervisor with restart
  backoff (max 5 per 10 min, then stay down), readiness flag.
- **REQ / design**: DD-001, REQ-331 (drain), Tech Design §3.3 error table
- **Depends on**: P3-06.2
- **Done**: submit from the main loop resolves after commit; a crash restarts with backoff and
  stops after 5 in 10 min; stop drains the queue.
- [ ]

### P3-07.2 · Engine jobs and settings cache
- **What**: `ingest/jobs.py`: finalizer every minute (Q12), purge schedule under `job_lock`,
  settings/pricing/session-key snapshots refreshed on `settings_changed` and periodically.
- **REQ / design**: REQ-032.b/c, REQ-037.c, REQ-092.a, REQ-006.c, DD-007
- **Depends on**: P3-07.1, P2-09.1, P2-10.2
- **Done**: fake-clock tests for job cadence; a second job holder skips; a settings change is
  visible to the normalizer within one refresh.
- [ ]

### P3-08.1 · Admission: API keys and auth cache
- **What**: `ingest/admission.py`: key from `X-Agentlens-Key` > `Authorization: Bearer` (gRPC
  metadata alike), parse `al_<id>_<secret>`, constant-time verify, positive cache ≤ 5 s cleared on
  `settings_changed`, `touch_api_key` at most once per minute.
- **REQ / design**: REQ-006.a–c, REQ-320.a, DD-014
- **Depends on**: P3-07.2
- **Done**: missing / malformed / unknown / revoked → 401 reason; revoked key rejected within 5 s
  (fake clock) and immediately after `settings_changed`.
- [ ]

### P3-08.2 · Limits: token buckets, body caps, overload
- **What**: `ingest/limits.py`: per-Project request and span token buckets (`Retry-After` ≥ 1 s),
  Content-Length precheck, streaming gzip with a decompressed cap (zip-bomb safe), overload check
  (queue bytes, commit latency) → 503 with `overload_retry_after`.
- **REQ / design**: REQ-007.a–c, REQ-009.b, DD-015
- **Depends on**: P3-08.1
- **Done**: bucket math with a fake clock; a 1 GiB zip bomb stops at the cap without allocating
  it; overload threshold flips 503 on and off.
- [ ]

### P3-09.1 · OTLP/HTTP routes
- **What**: `ingest/http.py` (Starlette): `POST /v1/traces`, `/v1/metrics`; `/v1/logs` → 404;
  protobuf and JSON in, same encoding out; `partialSuccess`; error → status mapping of API §3.3
  (400/401/413/415/429/503 with `google.rpc.Status`, fixed messages); check order of API §3.3.
- **REQ / design**: REQ-001, REQ-002, REQ-005.a, REQ-007, REQ-008, REQ-009.b, REQ-010
- **Depends on**: P3-08.2
- **Done**: route tests with an in-memory store fake and a real SQLite backend; every row of the
  API §3.3 table has a test.
- [ ]

### P3-10.1 · OTLP/gRPC services
- **What**: `ingest/grpc.py`: `TraceService`, `MetricsService`, `LogsService` → `UNIMPLEMENTED`;
  gzip; key from metadata; status mapping (`UNAUTHENTICATED`, `RESOURCE_EXHAUSTED` ± `RetryInfo`,
  `UNAVAILABLE`, `INVALID_ARGUMENT`); port disabled by config.
- **REQ / design**: REQ-003, REQ-006.b, REQ-007, REQ-008, REQ-009.b
- **Depends on**: P3-08.2
- **Done**: tests with the official OTLP gRPC exporter against an ephemeral port; every status
  row covered.
- [ ]

### P3-11.1 · Metrics ingestion
- **What**: `ingest/metrics.py`: GenAI metric allowlist (Tech Design §5.3 "Metrics persisted"),
  data points → `MetricPoint`, other `gen_ai.*` stored generically, non-GenAI metrics discarded and
  counted (not rejections), invalid GenAI points in `rejectedDataPoints`.
- **REQ / design**: REQ-005.a/b, Amendment A2
- **Depends on**: P3-09.1
- **Done**: a mixed export stores only GenAI points; counters and histograms both handled;
  `partialSuccess` counts only invalid GenAI points.
- [ ]

### P3-12.1 · Property tests: order invariance and idempotency
- **What**: `tests/property/`: hypothesis permutations, batch splits and duplicates of fixture span
  sets through `normalize_batch` + `write_group` → identical derived views (snapshot), on SQLite
  and PG.
- **REQ / design**: REQ-037.a/b, REQ-038.a, REQ-365
- **Depends on**: P3-07.1
- **Done**: property suite green with the CI hypothesis profile on both backends.
- [ ]

### P3-12.2 · Golden normalizer fixtures
- **What**: `tests/golden/`: hand-written OTLP/JSON fixtures (missing parent, late root,
  conflicting session IDs, both dialects present, generic trace, PydanticAI, LangGraph,
  OpenInference) with expected derived rows; a regeneration flag for intentional changes.
- **REQ / design**: REQ-032.c, REQ-035.b, REQ-036.c, REQ-131 (data)
- **Depends on**: P3-03.3
- **Done**: every fixture's derived rows match the golden files; changing `MAPPING_VERSION`
  without regenerating fails.
- [ ]

### P3-13.1 · Integration tests on real endpoints
- **What**: `tests/integration/`: the engine with HTTP and gRPC on ephemeral ports and a real
  backend: auth, rate limits, zip bomb, backpressure 503, DB outage → 503 then recovery ≤ 10 s,
  `BatchRejected` when a Project is deleted mid-ingest (A-04).
- **REQ / design**: REQ-001…REQ-010, REQ-009.a/b, REQ-330
- **Depends on**: P3-09.1, P3-10.1
- **Done**: suite green on SQLite and PG in CI.
- [ ]

### P3-13.2 · Durability and cross-process tests; phase gate
- **What**: SIGKILL-after-ack test (subprocess engine, every acknowledged span present after
  restart); revocation ≤ 5 s across two processes on PG; P3 gate.
- **REQ / design**: REQ-009.a, REQ-006.c, REQ-004
- **Depends on**: P3-13.1
- **Done**: plan P3 Done criteria: ingest + store coverage ≥ 85 %; `mypy --strict` clean; import
  contracts kept; property, golden and integration suites green in CI.
- [ ]

**Suggested batches:** (1) P3-01.1, P3-01.2, P3-01.3 · (2) P3-02.1, P3-02.2, P3-04.1 ·
(3) P3-03.1, P3-03.2, P3-04.2 · (4) P3-03.3, P3-05.1, P3-05.2 · (5) P3-06.1, P3-06.2, P3-07.1 ·
(6) P3-07.2, P3-08.1, P3-08.2 · (7) P3-09.1, P3-10.1, P3-11.1 · (8) P3-12.1, P3-12.2, P3-13.1,
P3-13.2.

---

## Phases P4–P10 — to be detailed at phase start

Each phase gets its atomic tasks in a new revision of this file when the previous phase's Done
criteria are met. The plan tasks, their scope, estimates and dependencies are fixed in the
[Implementation Plan](../plans/agentlens-phase-1.md) §3.

| Phase | Plan tasks | Detailed when | Prerequisite inputs |
|---|---|---|---|
| P4 Query | P4-01 … P4-06 | P2-07 done | — |
| P5 Runtime/CLI | P5-01 … P5-08 | P3-09 done | — |
| P6 UI | P6-01 … P6-13 | P4-02 done | S1 decisions (P0-06.1) |
| P7 Delivery | P7-01 … P7-06 | P6-11 done | PyPI trusted publisher, GHCR |
| P8 Recipes/bench | P8-01 … P8-06 | P3 done | Reference machine (P8-06) |
| P9 Docs/site | P9-01 … P9-06 | P6-11 done | Q-T1 (P9-05 only) |
| P10 Release | P10-01 … P10-05 | P9-04 done, P8-06 done | — |

---

## Traceability matrix (all PRD requirements → plan tasks)

Generated from the plan's "Implements" columns and checked against the PRD headings on 2026-10-31:
**103 requirements, 0 uncovered.** "Tests" lists the test files once they exist; until then the
column names the plan task whose Done criteria require the citing test. SHOULD/COULD requirements
are included; the criteria marked SHOULD/COULD inside MUST requirements are listed in the next table.

| REQ | Priority | Title | Plan tasks | Tests |
|---|---|---|---|---|
| REQ-001 | MUST | OTLP/HTTP trace ingestion, protobuf | P3-02, P3-09, P3-13 | pending |
| REQ-002 | MUST | OTLP/HTTP trace ingestion, JSON | P3-02, P3-09, P3-13 | pending |
| REQ-003 | MUST | OTLP/gRPC trace ingestion | P3-10, P3-13 | pending |
| REQ-004 | MUST | Ingestion in both process topologies | P0-04, P3-13, P5-01, P5-08 | pending |
| REQ-005 | MUST | OTLP metrics ingestion for GenAI metrics | P3-09, P3-11, P3-13 | pending |
| REQ-006 | MUST | API-key authentication per Project | P3-08, P3-10, P3-13 | pending |
| REQ-007 | MUST | Rate limiting and payload limits | P3-08, P3-09, P3-10, P3-13 | pending |
| REQ-008 | MUST | Unimplemented signals | P3-09, P3-10, P3-13 | pending |
| REQ-009 | MUST | Durable acknowledgement and backpressure | P3-06, P3-09, P3-10, P3-13 | pending |
| REQ-010 | MUST | Partial success | P3-02, P3-09, P3-13 | pending |
| REQ-030 | MUST | Span persistence with raw attributes | P3-02 | pending |
| REQ-031 | MUST | Source identification | P3-02 | pending |
| REQ-032 | MUST | Run derivation | P3-03, P3-07, P3-12 | pending |
| REQ-033 | MUST | LLMCall, ToolCall and Approval derivation | P3-03 | pending |
| REQ-034 | MUST | Message content in both attribute and event forms | P3-01 | pending |
| REQ-035 | MUST | Declarative semconv mapping table | P3-01, P3-12 | pending |
| REQ-036 | MUST | Session derivation | P3-03, P3-12 | pending |
| REQ-037 | MUST | Out-of-order and late spans | P2-05, P3-07, P3-12, P4-02, P6-05 | pending |
| REQ-038 | MUST | Idempotent ingestion | P2-05, P3-12 | pending |
| REQ-039 | MUST | Redaction before persistence | P3-04, P4-05, P6-10 | pending |
| REQ-040 | MUST | Content storage toggle per Project | P3-04 | pending |
| REQ-041 | SHOULD | Value size limits | P3-02 | pending |
| REQ-042 | MUST | Rebuild derived views | P5-06 | pending |
| REQ-060 | MUST | Versioned pricing table | P2-08, P3-05, P10-05 | pending |
| REQ-061 | MUST | Cost computation | P3-05, P4-03 | pending |
| REQ-062 | SHOULD | Cost recomputation | P4-05 | pending |
| REQ-070 | MUST | Storage protocol and contract suite | P2-03, P2-04, P2-11 | pending |
| REQ-071 | MUST | SQLite backend (default) | P2-05 | pending |
| REQ-072 | MUST | PostgreSQL backend | P2-06, P2-11, P7-04 | pending |
| REQ-073 | MUST | Full-text search | P2-05, P2-06, P2-11, P3-03 | pending |
| REQ-074 | MUST | Schema migrations | P2-02, P2-11, P5-02 | pending |
| REQ-090 | MUST | Retention by age | P2-09 | pending |
| REQ-091 | MUST | Retention by size | P2-09 | pending |
| REQ-092 | MUST | Purge scheduling | P2-09, P3-07 | pending |
| REQ-100 | MUST | Run list | P2-07, P4-01, P4-02 | pending |
| REQ-101 | MUST | Run filters | P2-07, P4-01 | pending |
| REQ-102 | MUST | Saved filters | P2-07, P2-08, P4-01, P6-04 | pending |
| REQ-103 | MUST | Trace detail and span tree | P2-07, P4-02, P6-05 | pending |
| REQ-104 | MUST | Normalized LLM I/O access | P2-07, P4-02 | pending |
| REQ-105 | MUST | Session queries | P2-07, P4-02, P6-08 | pending |
| REQ-106 | MUST | Dashboard aggregates computed in SQL | P2-04, P2-07, P4-03, P6-09 | pending |
| REQ-107 | SHOULD | Metrics aggregates | P2-07, P4-03, P6-09 | pending |
| REQ-130 | MUST | Shared page chrome | P6-01, P6-02, P6-09 | pending |
| REQ-131 | MUST | Generic (non-GenAI) traces | P3-12, P6-04, P6-05 | pending |
| REQ-140 | MUST | Live updates channel | P2-10, P4-06 | pending |
| REQ-141 | MUST | Dashboard | P6-09 | pending |
| REQ-145 | MUST | Runs table | P6-04 | pending |
| REQ-150 | MUST | Waterfall timeline | P6-05 | pending |
| REQ-151 | MUST | Span detail drawer | P6-06 | pending |
| REQ-152 | MUST | Token and cost breakdown | P4-02, P6-06 | pending |
| REQ-153 | MUST | Graph view | P4-04, P6-07 | pending |
| REQ-154 | MUST | Replay mode | P6-07 | pending |
| REQ-160 | MUST | Sessions list and detail | P6-08 | pending |
| REQ-170 | MUST | Projects and API keys | P2-08, P4-05, P6-10 | pending |
| REQ-171 | MUST | Pricing editor | P4-05, P6-10 | pending |
| REQ-172 | MUST | Redaction rules editor | P3-04, P4-05, P6-10 | pending |
| REQ-173 | MUST | Retention and content settings | P4-05, P6-10 | pending |
| REQ-174 | MUST | Admin gating | P4-05, P6-10 | pending |
| REQ-190 | MUST | Embeddable pages | P6-03, P6-12 | pending |
| REQ-191 | MUST | Host authentication hook | P4-05, P6-03 | pending |
| REQ-192 | MUST | Standalone app | P6-11 | pending |
| REQ-193 | MUST | Content-Security-Policy for embedded pages | P0-05, P6-11, P6-12, P9-02 | pending |
| REQ-200 | MUST | CLI | P0-03, P6-11, P7-01, P8-02 | pending |
| REQ-201 | MUST | First-run bootstrap | P5-02 | pending |
| REQ-202 | MUST | Configuration surface | P1-07 | pending |
| REQ-203 | MUST | Container image | P7-02 | pending |
| REQ-204 | MUST | `compose.yaml` (try-it) | P7-03 | pending |
| REQ-205 | MUST | `compose.postgres.yaml` (production) | P2-06, P7-04 | pending |
| REQ-206 | MUST | Publishing | P7-05, P10-05 | pending |
| REQ-210 | MUST | 60-second time-to-first-Run | P7-01, P7-03, P10-05 | pending |
| REQ-224 | MUST | Standalone UI authentication | P5-04 | pending |
| REQ-230 | MUST | Landing page | P9-04 | pending |
| REQ-231 | MUST | Live demo | P4-06, P6-10, P8-02, P9-04 | pending |
| REQ-232 | MUST | Docs and changelog | P9-01, P9-04 | pending |
| REQ-233 | MUST | Sample dataset generator | P8-01, P8-02 | pending |
| REQ-250 | MUST | Recipe set | P8-03, P9-02 | pending |
| REQ-251 | MUST | Shared attribute vocabulary for sibling projects | P9-06 | pending |
| REQ-260 | MUST | Recipe golden integration tests | P8-03, P8-04 | pending |
| REQ-300 | MUST | Ingest throughput, SQLite single process | P8-06 | pending |
| REQ-301 | MUST | Ingest throughput, PostgreSQL collector | P8-06 | pending |
| REQ-302 | MUST | Ingest latency | P8-06 | pending |
| REQ-303 | MUST | Runs page latency | P0-09, P2-07, P8-06 | pending |
| REQ-304 | MUST | Trace page latency | P0-09, P2-07, P8-06 | pending |
| REQ-305 | MUST | Dashboard latency | P0-09, P2-07, P8-06 | pending |
| REQ-306 | MUST | Live update latency | P8-06 | pending |
| REQ-307 | SHOULD | Resource footprint | P8-06 | pending |
| REQ-320 | MUST | API keys hashed at rest | P1-06, P2-08, P3-08, P10-01 | pending |
| REQ-321 | MUST | Admin credential handling | P5-04 | pending |
| REQ-322 | MUST | Redaction before persistence, verified | P10-01 | pending |
| REQ-323 | MUST | No content in logs or self-telemetry | P1-08, P10-01 | pending |
| REQ-324 | MUST | No unconfigured outbound traffic | P0-05, P6-12, P10-01 | pending |
| REQ-325 | MUST | Input validation at the edge | P2-03, P4-01 | pending |
| REQ-326 | MUST | Container hardening | P7-02 | pending |
| REQ-327 | MUST | Safe content rendering | P6-06, P6-12 | pending |
| REQ-328 | MUST | Supply-chain checks in CI | P1-03 | pending |
| REQ-330 | MUST | Database unavailability | P3-06, P3-13, P6-01 | pending |
| REQ-331 | MUST | Graceful shutdown | P5-05 | pending |
| REQ-340 | MUST | Health endpoints | P5-01 | pending |
| REQ-341 | MUST | Structured logs | P1-08 | pending |
| REQ-342 | SHOULD | Opt-in self-telemetry | P5-07 | pending |
| REQ-360 | MUST | Static quality gates | P1-02 | pending |
| REQ-361 | MUST | Test matrix and coverage | P1-02, P1-03, P10-02 | pending |
| REQ-362 | MUST | Traceability of tests | P1-05, P10-02 | pending |
| REQ-363 | MUST | Compile/smoke tests | P6-12, P9-04 | pending |
| REQ-364 | MUST | Docker test | P7-03, P7-04 | pending |
| REQ-365 | MUST | Property tests | P3-12 | pending |
| REQ-366 | MUST | Benchmark suite | P8-05 | pending |
| REQ-380 | MUST | Repository documentation | P1-04, P9-03 | pending |
| REQ-381 | MUST | User documentation | P9-01, P9-02 | pending |

### Non-MUST criteria inside MUST requirements

These are excluded from the blocking traceability check (P1-05.1) and may slip to v0.1.x with a
CHANGELOG note (PRD conventions). They still have a plan task.

| Criterion | Priority | Plan task |
|---|---|---|
| REQ-033.c (MCP `elicitation/create` part only) | COULD | P3-03 |
| REQ-060.d | COULD | P3-05 |
| REQ-141.c | SHOULD | P6-09 |
| REQ-141.e | COULD | P6-09 |
| REQ-150.c | SHOULD | P6-05 |
| REQ-160.c | SHOULD | P6-08 |
| REQ-171.c | SHOULD | P4-05, P6-10 |
| REQ-172.b | SHOULD | P3-04, P4-05, P6-10 |
| REQ-301.b | SHOULD | P8-06 |
| REQ-303.b | SHOULD | P8-06 |

SHOULD requirements as a whole: REQ-041, REQ-062, REQ-107, REQ-307, REQ-342 (all have plan tasks; see
the matrix). No requirement is deferred at this point.

---

## Execution log

| Date | Tasks | Result | Notes |
|---|---|---|---|
| 2026-10-31 | P0-01.1 | Done | Scaffold, `uv sync`, CLI stub, 3 unit tests (PR #2) |
| 2026-10-31 | P0-02.1, P0-02.2 | Done | Export works; 28 MB bundle (Mermaid icons/ZenUML/ELK); telemetry defaults on; hook needs `PYTHONPATH=src`; sdist question open for P7-01 (`spikes/s1/NOTES.md`) |
| 2026-10-31 | P0-03.1, P0-03.2 | Done | Wheel boots with no JS runtime; no foreign requests; baked `localhost:8000` rewritten per port (A6 not needed); start→200 6.3 s; port-in-use gives raw Granian traceback → CLI pre-check (REQ-200.c) |
| 2026-10-31 | P0-07.1 | Done | SQLite 3.51.0 + FTS5 contentless_delete, PG 18.6; one MetaData via `with_variant`/`ddl_if`; FTS5 shadow tables must be excluded from Alembic autogenerate (`spikes/s2/NOTES.md`) |
| 2026-10-31 | P0-04.1, P0-04.2 | Done | Granian spawns a worker process → ingest thread started by a Reflex lifespan task; 10k/10k spans per protocol; UI p95 unchanged at ~34k spans/s; SIGTERM: ordered shutdown, exit 0, ports released |
| 2026-10-31 | P0-05.1, P0-05.2, P0-05.3 | Done | Hash CSP enforced with 0 violations and no `unsafe-eval`; hashes stable per request, not per build; 0 foreign requests; Reflex writes `assets/external` and `.states` into the cwd at import |
| 2026-10-31 | P0-07.2, P0-08.1, P0-08.2 | Done | 1M load 57 s / 114 s; 16.0k / 7.5k spans/s sustained; order invariance identical on and across backends |
| 2026-10-31 | P0-09.1, P0-09.3 | Done | Run/trace queries ≤ 10 ms p95; FTS parity requires document normalization (applied to DD-006) |
| 2026-10-31 | P0-09.2 | Measured | REQ-305: PG 724 ms ✓, SQLite 3.3 s ✗ (percentile panel 2.5 s); values identical → P0-10.1 decision |
| 2026-10-31 | P0-06.1, P0-10.1 | In review | Tech Design v1.2 + Data Model v1.0.1 spec PR; REQ-305 option to be chosen by the maintainer |
| 2026-10-31 | P1-01.1 … P1-08.1 | Done | PRs #6–#9; quality gates, CI, guidance, traceability, `core` |
| 2026-10-31 | P2-01.1, 01.2, 02.1, 02.2 | Done | PR #11; schema for both backends, Alembic `0001_initial`, guarded migration |
| 2026-10-31 | P2-03.1 … P2-07.1 | Done | PR #12; protocol, SQLite and PG `write_batch`, multi-writer test, run/trace reads; PG text ordering forced byte-wise (`COLLATE "C"`) |
| 2026-10-31 | P2-07.2, 07.3, 07.4, 08.1 | Done | PR #13; Sessions, aggregates (auto bucket minute ≤ 2 h, hour ≤ 3 d), metric points (inference counters preferred over the legacy histogram), Projects and API keys |
| 2026-10-31 | P2-08.2, 08.3, 09.1 | Done | PR #14; pricing versions, finalizer (`SKIP LOCKED`), rebuild, cost recompute, retention by age/size, job locks (PG advisory); retention age clamped to avoid BIGINT overflow |
| 2026-10-31 | P2-10.1, 10.2, 11.1 | Done | PR #15; change feed (after-commit publish, PG NOTIFY in the commit transaction, LISTEN with origin/namespace filtering, fingerprint polling fallback); parity SQLite↔PG on runs, search sets, aggregates, percentiles, Sessions; `scripts/check_upgrade_head.py` in CI. **P2 gate**: 681 passed, `store` coverage 95 %. Finding: tied Sessions are ordered by the Session ULID (surrogate), so tie order differs across databases |
| 2026-10-31 | P3 prep | Done | PR #16; Delta `changes/2026-10-p3-ingest-prep/` (A-04, A-07, A-08, A-17) folded; P3 detailed |
| 2026-10-31 | P3-01.1, 01.2, 01.3 | Done | PR #17; `SEMCONV_GENAI_COMMIT` = `6f98aef` (genai repo `main`, no release; closes Q-T2); mapping table + classification + dialects; messages normalizer (5 forms). Security review: JSON `RecursionError`, bounded list indices, content-drop prefixes widened beyond Tech Design §5.3 (fold later) |
| 2026-10-31 | P3-02.1, 02.2, 04.1 | Done | PR #18; OTLP decode (protobuf/JSON, hex IDs), validation + `partialSuccess`; span drafts, truncation, classification; redaction presets (linear-time email), pattern/field rules, fail-closed timeouts, A-07 canary. Security review: `$b64`-shaped kvlists no longer bypass redaction/truncation |

Suggested first batch (after Analyze): **P0-01.1, P0-02.1, P0-02.2, P0-07.1** (the last one in
parallel, on the S2 branch).

---

## Constitution check

| Article | How these tasks comply |
|---|---|
| Art. 1 | No ingest code in P0/P1; the mapping table is a P3 task with its own Done criteria |
| Art. 2 | S2 tests plain SQL on SQLite and PG only; no new infrastructure; any DuckDB move requires a Delta Spec (P0-10.1) |
| Art. 3 | P0-05.2 verifies zero third-party requests; P1-08.1 enforces the content guard from day one |
| Art. 4 | Key hashing primitives (P1-06.3), secrets via env/`*_FILE` (P1-07.2), gitleaks + pip-audit in CI (P1-03.2), CSP feasibility (P0-05.1) |
| Art. 5 | `core.money` refuses floats (P1-06.2); order invariance checked early (P0-08.2) |
| Art. 6 | `mypy --strict` and import contracts in place before any layer code (P1-02.1, P1-02.2) |
| Art. 7 | S1 validates silverpoint and mermaidjs in the pre-built bundle (P0-02.1, P0-05.2) |
| Art. 8 | Test-first rule stated; every `core` task's Done names the `REQ` its tests cite; coverage gate and traceability script wired in P1 |
| Art. 9 | Not exercised in P0/P1 beyond JSON logs (P1-08.1); health endpoints are P5-01 |
| Art. 10 | Spikes before dependent work; spec edits from spikes go through approval (P0-06.1, P0-10.1); branches from `develop`; spike branches never merged; all in English |

**Exceptions requested:** none. **Process deviation recorded:** rolling-wave detailing of P2–P10
(see "How this file works"); coverage is still checked for the whole release via the matrix.

---

## Change history

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0-draft.1 | 2026-10-31 | Ernesto Crespo (with Claude) | Atomic tasks for P0 and P1; full-release traceability matrix (103 REQs, 0 uncovered) |
| 1.0 | 2026-10-31 | Ernesto Crespo | Approved (rolling-wave detailing accepted) |
| 1.0.1 | 2026-10-31 | Ernesto Crespo (with Claude) | Analyze fixes: A-03 (`percentile_disc` in P0-09.2), A-13 (removed [P] from P0-05.1/05.2, P1-02.1/02.2/02.3: shared files), A-15 (new P0-09.3), A-16 (new P0-05.3) |
| 1.1 | 2026-10-31 | Ernesto Crespo (with Claude) | Phase P2 detailed: 21 atomic tasks, protocol-method ownership table (A-06), protocol return types as `store.records` (A-05, storage side), A-01 multi-writer test (P2-06.2), no pricing seed in `0001_initial` (A-11) |
| 1.2 | 2026-10-31 | Ernesto Crespo (with Claude) | Phase P3 detailed: 25 atomic tasks in 8 batches; store additions `write_group` / `get_or_create_secret` owned by P3-06.1; Delta `changes/2026-10-p3-ingest-prep/` (A-04, A-07, A-08, A-17) folded into API v1.3, Tech Design v1.4, Data Model v1.2 |
