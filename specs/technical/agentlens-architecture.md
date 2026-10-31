# reflex-agentlens — Technical Design Document

## Metadata

| Field | Value |
|---|---|
| **Author** | Ernesto Crespo (`ecrespo`) |
| **Status** | `APPROVED` |
| **Version** | 1.3 |
| **Date** | 2026-10-31 |
| **Related PRD** | [`specs/prd/agentlens-v0.1.md`](../prd/agentlens-v0.1.md) v1.0 |
| **Related API Spec** | [`specs/api/agentlens-api-v1.md`](../api/agentlens-api-v1.md) v1.0 |
| **Governed by** | [`specs/constitution.md`](../constitution.md) v1.0 |
| **Reviewers** | Ernesto Crespo |

**Research basis (2026-10-30).** The facts below were checked against PyPI metadata, the
downloaded wheel sources of `reflex` 0.10.1, `pydantic-ai-slim` 2.64.0,
`openinference-instrumentation-*`, `langsmith` 0.14.16 and `langgraph` 1.2.19, the
`open-telemetry/semantic-conventions` v1.44.0 changelog, the
`open-telemetry/semantic-conventions-genai` repo (`main`), and the silverpoint catalog (MCP
server, silverpoint 0.8.0). Anything not verified is marked **[spike]** and assigned to a spike
in §11.

---

## 1. Context

agentlens is a single Python package that bundles an OTLP collector, an SQL store and a Reflex
UI. The engineering difficulties are not in any one piece but in where they meet:

1. **Ingestion and UI share a process by default.** Reflex 0.10 runs its backend as a Starlette
   ASGI app under Granian, with a single worker when there is no Redis. OTLP/HTTP, OTLP/gRPC,
   a CPU-heavy normalizer, SQL writes and UI websocket traffic must share that process without
   starving each other.
2. **The domain model sits on moving specs.** GenAI conventions moved **out of core semconv
   in v1.42.0** into `semantic-conventions-genai`. That repo has **no release yet** (schema
   `gen-ai-dev/1.42.0-dev`, all Development). Frameworks emit at least five dialects: spec
   `gen_ai.*`, deprecated `gen_ai.*`, OpenInference `llm.*`, LangSmith `langsmith.*`, and
   PydanticAI extras.
3. **A pre-built frontend is required** so that `uvx reflex-agentlens` needs no Node.js.
   Reflex 0.10 supports single-port prod serving natively, but driving it from a pre-built bundle
   inside a wheel relies on internal environment variables.
4. **Two SQL backends must return the same results**, including full-text search and
   percentiles, which SQLite lacks natively.

## 2. Technical goals

| Goal | Concrete target | Source |
|---|---|---|
| Correctness | Derived views independent of span arrival order and batching. Idempotent ingest. Durable acknowledgement | REQ-009, 037, 038 |
| Performance | ≥ 2,000 spans/s SQLite, ≥ 5,000 spans/s per PG collector. Runs p95 < 300 ms at 100k spans. Dashboard < 1.5 s (SQLite) / < 1 s (PG) at 1M spans | §7.1 PRD |
| Privacy | Redaction and content dropping happen in the ingest engine before any write. Canary tests | REQ-039, 040, 322, 323 |
| Maintainability | UI-free core (`ingest`/`store`/`query`), mypy strict, ≥ 85 % coverage, one declarative mapping table | Art. 6, 8 |
| Operability | `/healthz` and `/readyz`, JSON logs, opt-in self-telemetry, bounded memory, graceful shutdown | REQ-331, 340–342 |

---

## 3. Proposed architecture

### 3.1 Process internals (one agentlens process)

```
                      ┌──────────────────────── agentlens process (serve) ─────────────────────────┐
 browser ──ws /_event─┼─▶ MAIN LOOP (Granian worker, asyncio)                                       │
 browser ──GET /*  ───┼─▶  Starlette root                                                           │
                      │    ├─ /healthz /readyz /_agentlens/auth/*      (agentlens routes)           │
 OTLP/HTTP ──POST ────┼─▶  ├─ /v1/traces /v1/metrics /v1/logs ──┐      (agentlens routes)           │
                      │    ├─ Reflex API (/_event, /_upload, /ping…)  │                             │
                      │    └─ StaticFiles(pre-built frontend)         │ submit(batch) →              │
                      │                                              │ concurrent.futures.Future   │
                      │    Reflex states ──▶ QueryService ──▶ READ engine (pool)                    │
                      │         ▲  background task per client: QueryService.subscribe()             │
                      │         └──────────── ChangeFeed (in-process broadcaster) ◀────────┐       │
                      │                                                                    │       │
                      │  INGEST THREAD (own asyncio loop)                                  │       │
 OTLP/gRPC :4317 ─────┼─▶ grpc.aio server ──┐                                              │       │
                      │                     ▼                                              │       │
                      │   Admission (auth cache, rate limiter, size limits)                │       │
                      │                     ▼                                              │       │
                      │   Normalizer (decode → validate → truncate → redact → derive →     │       │
                      │               cost → search doc)                                   │       │
                      │                     ▼                                              │       │
                      │   bounded WriteQueue (bytes budget) ──▶ Writer (group commit,      │       │
                      │                                         WRITE engine, 1 conn)      │       │
                      │                                              │  commit ─▶ publish ──┘       │
                      │   Jobs: finalizer (1/min), purge (interval), settings-cache refresh │       │
                      │   [PG only] LISTEN agentlens_changes ─────────▶ ChangeFeed          │       │
                      └─────────────────────────────────────────────────────────────────────────────┘
```

`collect` runs the same ingest thread plus a minimal Starlette app (health and OTLP/HTTP routes),
without Reflex.

### 3.2 Components

| Component | Package / module | Technology | Responsibility |
|---|---|---|---|
| CLI | `reflex_agentlens.cli` | stdlib `argparse` | Commands and exit codes (API §8.3) |
| Config | `core.config` | `pydantic-settings` | Defaults < TOML < env < flags. Unknown-key detection. `*_FILE` secrets |
| Security primitives | `core.security` | stdlib `secrets`, `hashlib`, `hmac` | Key generation and hashing, cookie and cursor signing |
| Semconv mapping | `core.semconv` | pure Python data | Pinned constants plus the declarative mapping table (§5.3) |
| Ingest engine | `ingest.engine` | dedicated thread plus `asyncio` loop | Hosts gRPC, admission, normalizer, writer, jobs |
| OTLP/HTTP | `ingest.http` | Starlette routes | Decode the HTTP envelope and hand off to the engine |
| OTLP/gRPC | `ingest.grpc` | `grpcio` (`grpc.aio`) | Trace and metrics services, logs → UNIMPLEMENTED |
| Normalizer | `ingest.normalize` | `opentelemetry-proto` (upb protobuf) | Span → `NormalizedSpan` (+ derived rows) |
| Redaction | `ingest.redaction` | `regex` (with timeout) | Presets, pattern and field rules, hook |
| Cost | `ingest.cost` | `decimal` | Pricing match and cost |
| Store | `store.sqlite`, `store.postgres` | SQLAlchemy 2.0 Core (async), `aiosqlite`, `asyncpg`, Alembic | `StorageBackend` implementations |
| Change feed | `store.changefeed` | asyncio broadcaster plus PG `LISTEN/NOTIFY` | Live notifications (§4 DD-007) |
| Query layer | `query` | Pydantic v2 DTOs | `QueryService`, `AdminService`, URL grammar, graph assembly |
| UI | `components`, `pages` | Reflex 0.10, `reflex-silverpoint-react`, `reflex-mermaidjs` | Pages, waterfall, drawer, charts, graph |
| App wiring | `app` (`mount`, `standalone`, `lifespan`, `auth`, `csp`) | Reflex `register_lifespan_task`, `api_transformer` | Embedding API and standalone shell |
| Server | — | Granian (Reflex's prod server) **[spike S1]** | ASGI hosting, single worker |

### 3.3 Data flows

**Flow 1: OTLP/HTTP export (happy path)** [REQ-001, 002, 006, 007, 009, 039, 061]

```
 1. Starlette route checks Content-Length ≤ max_body_bytes        → 413 otherwise
 2. Content-Type / Content-Encoding checked                       → 415
 3. API key parsed (X-Agentlens-Key > Authorization: Bearer); verified via the auth cache
    (TTL ≤ 5 s) or the DB (lookup by key_id, constant-time hash compare) → 401
 4. Request token bucket for the Project                          → 429 + Retry-After
 5. Body streamed with a running byte count; gzip decompressed incrementally with a cap → 413
 6. Raw bytes plus metadata submitted to the ingest loop
    (run_coroutine_threadsafe); the HTTP handler awaits the wrapped future
 7. [ingest loop] decode ExportTraceServiceRequest (protobuf, or JSON via json_format) → 400
 8. Span token bucket (by decoded span count)                     → 429
 9. Per span: validate → truncate long values → redaction (rules, then hook)
    → content drop (if the Project disabled it) → derive LLMCall/ToolCall/Approval/GraphNode
    → cost (pricing snapshot) → search document. Invalid spans are counted as rejected
10. Enqueue NormalizedBatch in the WriteQueue (byte budget)        → 503 if full
11. Writer groups queued batches (≤ 50 ms or ≤ 5,000 spans) into ONE transaction:
    write_batch() semantics (API §5.1): insert-or-ignore spans, derived rows,
    recompute affected Runs and Sessions in SQL
12. COMMIT. Each batch's future is resolved with its WriteResult
13. ChangeFeed.publish() (in-process; plus NOTIFY on PG, in the same transaction)
14. Handler encodes ExportTraceServiceResponse (partialSuccess if rejections) → 200
```

gRPC follows the same path from step 3. Interceptor-free: the servicer does the steps inline,
reading the key from metadata.

**Flow 2: live update** [REQ-140, 145.c]

```
1. Page on_load starts @rx.event(background=True, supersedes=True) subscribe_loop
2. Loop: async for notice in QueryService.subscribe(viewer, projects, min_interval=1s)
3.   if client token no longer connected (app.event_namespace.token_to_sid) → exit
4.   re-run the page's query (e.g. first page of runs) → async with self: update vars
5. A 30-min hard lifetime plus re-subscribe on navigation (supersedes) bounds leaks
```

**Flow 3: trace page load** [REQ-103, 150–154]: `on_load` → `resolve_trace` → `get_trace`. The
first 2,000 SpanNodes in DFS order are loaded into state, with subtrees deeper than 3 collapsed.
"Load more" fetches further pages. `get_span_raw` runs lazily when the Raw tab or drawer opens.
`AgentGraph` → Mermaid text is built in `components.graph`.

**Error and compensation flows**

| Failure | Handling |
|---|---|
| DB unreachable at commit | Writer fails the whole group. Futures resolve to `StorageUnavailable` → 503 / `UNAVAILABLE` (retryable). `/readyz` turns not ready. The writer retries `ping()` with backoff 0.5 → 5 s and resumes automatically [REQ-330] |
| Commit latency p50(5 s) > threshold, or queue bytes > budget | Admission returns 503 before decoding [REQ-009.b] |
| Redaction regex timeout | Value replaced by `[REDACTED:timeout]`, counter incremented [REQ-039.d] |
| Redaction hook raises | Span rejected with `redaction_hook_error` [REQ-039.e] |
| Ingest thread crash (bug) | Supervisor in the main loop sees the thread die. `/readyz` → 503, ingest → 503, CRITICAL log, restart with backoff (max 5 per 10 min, then stay down) |
| SIGTERM | Admission closed (503). The writer drains the queue for up to `shutdown_grace`, then the gRPC server stops, then the main loop exits [REQ-331] |
| PG LISTEN connection lost | ChangeFeed falls back to polling `max(committed_at)` every 2 s until reconnected (DD-007) |

---

## 4. Design decisions

### DD-001: Process topology and where the receiver runs (PRD Q1)

- **Decision:** In `serve`, OTLP/HTTP runs as **Starlette routes on the Reflex backend port**,
  and **OTLP/gRPC plus the whole ingest pipeline run in a dedicated "ingest thread" with its own
  asyncio loop** inside the same process. `collect` runs the identical ingest thread under a
  minimal Starlette app with no Reflex.
  **S1 finding:** Granian's `Server` runs the app in a spawned worker process even with one
  worker, so the ingest thread is started and stopped by a Reflex lifespan `asynccontextmanager`
  (`app.register_lifespan_task`), which runs inside the worker. Shutdown ordering (HTTP closed →
  lifespan exit → drain → gRPC stop → thread join) was verified in S1 (§11.1).
- **Context:** Reflex prod mode is single-port and single-worker without Redis (state manager
  defaults to DISK; `get_num_workers()` = 1). Granian may run the app on a non-stdlib event loop,
  and `grpc.aio` must not depend on that loop **[spike S1]**. Hot reload restarts the backend
  process and cancels lifespan tasks.

| Option | Pros | Cons |
|---|---|---|
| **A. Ingest thread with its own loop (chosen)** | gRPC isolated from Granian's loop. UI websocket latency protected from writer stalls. The same engine code serves `serve`, `collect` and embedded hosts | Cross-thread handoff. GIL contention (normalizer CPU still competes) |
| B. Everything on the Reflex main loop | Simplest | gRPC on Granian's loop is unverified. Long commits/normalization block UI events |
| C. Separate OS process for ingest in `serve` | No GIL contention | Two processes break "one process, zero infra". Cross-process change feed needed even on SQLite |

- **Rationale:** A is the only option that meets REQ-004 (one process, both protocols) without
  betting on gRPC under Granian's loop, and it keeps UI responsiveness independent of ingest bursts.
- **Consequences:** The normalizer competes for the GIL. Mitigation: protobuf decode is C (upb)
  and the per-span Python work is kept allocation-light. The benchmark gate (REQ-300) decides
  whether to move normalization to a `ProcessPoolExecutor` (escape hatch, no API change). In dev
  (`reflex run`) hot reload restarts the ingest thread. The dev app defaults to
  `grpc_enabled=false` to avoid port-rebind races. Embedded hosts get the same ingest thread when
  they pass `mount(ingest=True)`.

### DD-002: Pre-built frontend in the wheel and single-port serving (PRD Q7, Q5)

- **Decision:** At **build time**, a custom hatchling build hook runs `reflex export
  --frontend-only --no-zip` on a build-only app module (`reflex_agentlens/_webapp/`) and packages
  `.web/build/client/**` as `reflex_agentlens/_frontend/`. This needs Bun 1.4.4 or Node ≥ 22.22
  **in CI only**. At **runtime**, `serve` does the following:
  1. Copies the bundle to `{data_dir}/web/<version>/`. Reflex writes `backend/*.json` into the
     web workdir, so it must be writable.
  2. Writes a minimal `rxconfig.py` there.
  3. Sets `REFLEX_WEB_WORKDIR`, `REFLEX_STATES_WORKDIR`, `REFLEX_ENV_MODE=prod`,
     `__REFLEX_MOUNT_FRONTEND_COMPILED_APP=true` and `__REFLEX_SKIP_COMPILE=true`.
  4. Serves `standalone_app()` through Granian's ASGI factory with one worker.

  The Reflex backend serves the static frontend itself (Reflex 0.9+ prod single-port behavior).
  **No Caddy in the single-container image.** Caddy appears only in `compose.postgres.yaml`,
  for TLS.
- **Context:** Reflex 0.10 `reflex run --env prod` always rebuilds the frontend and needs a JS
  runtime. The static mount is gated by an *internal* env var. `env.json` bakes `api_url` at
  compile time: on plain HTTP the browser keeps the **compiled port**; on HTTPS the port is dropped.

| Option | Pros | Cons |
|---|---|---|
| **A. Pre-built bundle plus internal Reflex env vars (chosen)** | Meets REQ-200.b and REQ-210.b. One port. No Node at runtime | Relies on internal env vars (`__REFLEX_*`) that can change in any Reflex release. Baked-port issue |
| B. Require Node/Bun at runtime and compile on first start | Officially supported path | Violates REQ-200.b. First start takes minutes, which breaks the 60 s promise |
| C. Static export served by Caddy, backend separate | Mature static serving | Two processes in the "single container". Still has the baked-URL issue |

- **Rationale:** A is the only option that satisfies the approved PRD. Its risks are contained
  by pinning Reflex to `>=0.10.1,<0.11`, by spike S1, and by a CI job that boots the installed
  wheel with **no `node`/`bun` on PATH** (REQ-200.b test).
- **Consequences:**
  1. **Baked port (resolved by S1):** the backend URLs are compiled into
     `assets/reflex-env-<hash>.js` (`localhost:8000`), not fetched at runtime. `serve` rewrites the
     origin in the copied workdir from `server.public_url`, or else from `http_host:http_port`, and
     deletes/regenerates the precompressed `.gz/.br/.zst` siblings (Reflex serves them in
     preference to the plain file). Verified on a non-default port (§11.1). Amendment A6 dropped.
  2. Each Reflex minor bump is a deliberate upgrade with S1's boot test re-run.
  3. Embedded hosts compile agentlens pages with their own toolchain, so they are unaffected.
  4. **Import side effects (S1, REQ-190.c):** importing `reflex_mermaidjs` creates
     `assets/external/…` symlinks in the **current directory**, and importing the app module creates
     `.states/` there. `serve` therefore `chdir`s to `{data_dir}/web/<version>/` and sets
     `REFLEX_STATES_WORKDIR` **before** importing Reflex. No network or subprocess at import.
  5. `serve` pre-checks the HTTP and gRPC ports with a bind test and exits with code 3 and a
     one-line message; Granian otherwise prints a raw traceback on `EADDRINUSE` (REQ-200.c).

### DD-003: Write path with durable group commit (REQ-009, REQ-300/301)

- **Decision:** There is **one writer coroutine per process**, which on SQLite is the only
  writer connection. It drains a bounded `WriteQueue` and commits each **group** (≤ 50 ms
  linger, ≤ 5,000 spans) in one transaction. Request futures resolve only after the commit.
  SQLite runs in WAL mode with `synchronous=NORMAL` and `busy_timeout=5000`. On PostgreSQL,
  commits are synchronous (default) and each writer has its own connection, so several `collect`
  replicas write concurrently. Multi-row inserts use `INSERT … ON CONFLICT DO NOTHING RETURNING`,
  which tells which spans are new.
- **Alternatives:**
  - Acknowledge on enqueue (faster, but loses data on crash; violates REQ-009.a).
  - Per-request transactions (simpler, but SQLite tops out at roughly hundreds of commits per
    second; fails REQ-300).
  - An external queue (violates Art. 2).
- **Consequences:**
  - `synchronous=NORMAL` in WAL mode survives process kill (REQ-009.a's SIGKILL test). It does
    not survive OS crash or power loss; this is documented in the operations docs.
  - Latency floor per request is about 50 ms (within the 250 ms p95 target, REQ-302).
  - Purge, finalizer and rebuild writes also go through the writer coroutine on SQLite, so writes
    are serialized and there are no `SQLITE_BUSY` storms.

### DD-004: Derived views computed from scratch per affected trace, in SQL (REQ-032, 036, 037)

- **Decision:** Per-span derivations happen in the normalizer. Run and Session rows are
  **recomputed from all stored rows of each affected trace** inside the commit transaction, by
  one set-based `INSERT … SELECT … GROUP BY trace … ON CONFLICT DO UPDATE` statement per group
  (affected trace IDs passed as a temp table or array). Status rules (REQ-032.b) are a SQL
  `CASE` over: root present, root status, max span time, and `now - finalization_window`. The
  periodic finalizer only re-evaluates `in_progress` Runs whose last span is older than the
  window.
- **Alternatives:** incremental counters (fast, but order-dependent and fragile under duplicates
  and late spans); recomputing in Python (violates Art. 6 and is slower).
- **Consequences:** Order and batching invariance hold by construction, which REQ-365 property
  tests verify. Cost per group is proportional to the size of the affected traces. Bounded
  because traces above 5,000 spans are recomputed incrementally in the same statement over
  indexed (`project_id`, `trace_id`) rows. Benchmarked in S2.
- **Concurrency rule (Analyze A-01).** Recompute-from-scratch is order-independent only if no two
  transactions recompute the same Run from different partial views. Under READ COMMITTED with
  several writers (`collect` ×N, plus the finalizer, purge, rebuild and cost recompute), two
  transactions can insert spans of the same trace concurrently, and the last commit would
  overwrite the Run with a partial view. Therefore every transaction that changes the spans of a
  Run, or recomputes it, SHALL:
  1. **Lock the affected Runs first, in a deterministic order.** The write path creates or locks
     the Run stubs in one statement, `INSERT … ON CONFLICT (project_pk, trace_id) DO UPDATE SET
     updated_at_ns = excluded.updated_at_ns RETURNING pk, trace_id`, with the rows sorted by
     (`project_pk`, `trace_id`). `DO UPDATE` takes the row lock that `DO NOTHING` does not. The
     finalizer, purge, rebuild and cost recompute use `SELECT … FOR UPDATE` on `runs` in the same
     order.
  2. Insert spans and derived rows, then recompute the Runs. A transaction that waited on a lock
     sees the spans the other committed, because every later statement takes a new snapshot.
  3. **Lock the affected Sessions** (old and new `session_pk`s) with `SELECT … FROM sessions WHERE
     pk IN (…) ORDER BY pk FOR UPDATE`, after the Runs, then recompute them.

  Lock order is always Runs (by `project_pk`, `trace_id`) before Sessions (by `pk`), so these
  transactions cannot deadlock with each other. If PostgreSQL still reports
  `deadlock_detected` (40P01), for example against an admin operation, the writer retries the
  whole group once, then fails its batches as retryable (503). SQLite needs no extra locking: one
  process holds the only write connection (`BEGIN IMMEDIATE`), and `collect` requires
  PostgreSQL. The P2 multi-writer contract test interleaves the **same** traces across writers.

### DD-005: Analytics engine, plain SQL without DuckDB (PRD Q2)

- **Decision:** **No DuckDB in v0.1.** Dashboards run plain SQL on the primary backend:
  - Narrow derived tables (`runs`, `llm_calls`, `tool_calls`, `graph_nodes`, `approvals`), so
    aggregates scan those rather than raw spans.
  - Covering indexes on (`project_id`, `start_ns`) plus grouping columns.
  - Portable window-function percentiles (`ROW_NUMBER() OVER (PARTITION BY g ORDER BY d)` with
    per-group counts) on SQLite, and `percentile_disc` on PostgreSQL (nearest-rank; never `percentile_cont`, which interpolates). Both are hidden behind
    one `percentile_sql()` builder and give identical results by nearest-rank definition.

  **Fallback ladder** if spike S2 misses REQ-305 on 1M spans:
  1. Hourly rollup tables maintained by the writer for counts and sums, with log-scale latency
     histograms (64 buckets) for percentiles. Still SQL, no new dependency.
  2. Re-evaluate DuckDB through a delta spec.
- **Alternatives:** DuckDB attached to the SQLite file. It is fast for scans, but adds a large
  native dependency, a second SQL dialect, file-locking interplay with the live SQLite writer,
  and nothing for PostgreSQL.
- **Consequences:** One dialect pair to maintain. The percentile definition is documented
  (nearest-rank), and contract tests compare both backends exactly.

### DD-006: Full-text search (REQ-073)

- **Decision:** One search document per span holds the span name, message text parts, tool
  arguments and results, and error/exception messages. It is built after redaction and truncated
  to 32 KiB.
  - **SQLite:** FTS5 contentless table, `tokenize='unicode61 remove_diacritics 2'`, rowid = span
    surrogate key.
  - **PostgreSQL:** `span_search(tsv tsvector)` built with `to_tsvector('simple', …)` plus a GIN index.
  - **Normalization (S2, Analyze A-15):** the document is stored already normalized by one pure
    function `search_normalize(text)`: NFKD, drop combining marks, lowercase, every run of
    non-letter/digit characters (including `_`, `.`, `-`) replaced by one space. Without it the
    backends disagree: PostgreSQL's parser keeps `db.query` as one host token and does not fold
    diacritics, FTS5 splits and folds.
  - **Queries:** the user input goes through the same `search_normalize`, is split on spaces into
    tokens and combined with AND. FTS5 gets `"t1" "t2"` (quoted); PostgreSQL gets
    `to_tsquery('simple', 't1 & t2')` built from those tokens only. Run matches use `EXISTS` on
    the trace. Identical result sets on both backends were verified for identifiers
    (`search_web`, `db.query`, `gpt-4o-mini`, `RefundTool`, accented words; §11.2).
- **Consequences:** No stemming and no prefix search in v0.1, identical on both backends for
  ASCII (REQ-073.b). Index size is bounded by the 32 KiB cap.

### DD-007: Live updates via in-process broadcaster plus PG LISTEN/NOTIFY (PRD Q4)

- **Decision:** `ChangeFeed` is an in-process asyncio fan-out (thread-safe publish from the
  ingest loop onto the main loop through `call_soon_threadsafe`). On PostgreSQL, the writer also
  runs `pg_notify('agentlens_changes', json)` **inside the commit transaction**, so delivery
  happens exactly on commit. The payload is ≤ 7,900 bytes; above that it is `truncated:true` with
  no run keys. Every `serve` process holds one dedicated `asyncpg` LISTEN connection that feeds
  the local broadcaster and drops self-originated events by `origin_id`. **SQLite with a
  separate `collect` stays unsupported** (`collect` exits 2 on SQLite).
- **Alternatives:**
  - Polling only (simple, but scaling costs grow with clients, and meeting ≤ 3 s means polling
    every ≤ 2 s per page).
  - Redis pub/sub (violates Art. 2).
  - SQLite `data_version` polling for cross-process use (adds complexity for a topology nobody needs).
- **Consequences:** ≤ 3 s p95 is easily met (typically < 200 ms). Lossy by design (API §5.2):
  subscribers re-query. A polling fallback covers a dropped LISTEN connection.

### DD-008: Persistence stack, SQLAlchemy 2.0 Core (async), no ORM on hot paths

- **Decision:** Tables are declared with SQLAlchemy Core (`Table`/`MetaData`) in `store.schema`.
  `aiosqlite` serves SQLite and `asyncpg` serves PostgreSQL. Migrations are Alembic (async
  `env.py`, `render_as_batch=True` for SQLite ALTERs) and ship inside the package. Dialect
  differences (`ON CONFLICT`, FTS, JSON operators, percentiles) live in a small per-backend
  `SqlDialectOps` class. **SQLModel is not used:** per-row Pydantic validation costs too much at
  2k–5k spans/s, and Reflex 0.10 made `sqlmodel` optional (`reflex[db]`), so we do not need it.
  agentlens never uses `rx.Model`, `rx.session` or `db_url`, so Reflex's own engine cache is
  never touched (REQ-071.b).
- **Constraints checked at startup:**
  - SQLite ≥ 3.43 with FTS5 enabled (contentless-delete FTS, JSON functions built in, window
    functions). Otherwise exit 2 with a message. Python 3.11+ official builds satisfy this
    (Data Model N2).
  - PostgreSQL ≥ 15 (`MERGE`, `NULLS NOT DISTINCT`). CI tests 15 and 18.
- **Consequences:** Constitution "SQLModel/SQLAlchemy" is satisfied by the SQLAlchemy branch.
  Entity definitions in the Data Model are expressed as Core tables.

### DD-009: Declarative multi-dialect semconv mapping (PRD Q3, REQ-035)

- **Decision:** One pinned **target model** and one **mapping table** in
  `core/semconv/mapping.py`, versioned as `MAPPING_VERSION` (int, bumped on any change), recorded
  per span (REQ-030.c).
  - **Target model:** OTel GenAI conventions from `semantic-conventions-genai` at a **pinned
    commit** (`SEMCONV_GENAI_COMMIT`, recorded when the module is implemented, schema
    `1.42.0-dev`) on top of core semconv **v1.44.0**.
  - **Dialects mapped onto it, in priority order:**
    1. Spec GenAI (genai repo `main`)
    2. Deprecated core GenAI (≤ v1.41, including `gen_ai.system`,
       `gen_ai.usage.prompt_tokens/completion_tokens`, `cache_creation`, event-based messages)
    3. PydanticAI extras
    4. OpenInference
    5. LangSmith OTel

  Full table in §5.3. The rationale is Constitution Art. 1, and REQ-035.b requires the spec
  attribute to win.
- **Consequences:** When the genai repo cuts a release, bumping the pin is a mapping change plus
  a `rebuild`. Unknown attributes are always kept raw.

### DD-010: Approval sources (PRD Q8)

- **Finding:** There is **no OTel convention for approvals or HITL**. LangGraph exposes
  `on_interrupt`/`on_resume` callbacks, but the OpenInference instrumentor implements them as
  no-ops and marks `GraphInterrupt` as OK. **LangGraph interrupts leave no attribute** in
  OpenInference traces.
- **Decision:** Approvals are derived from:
  1. The `reflex_agentkit.approval.*` vocabulary (§5.4), proposed upstream.
  2. PydanticAI deferred tools (`pydantic_ai.tool.deferral.name` / `.metadata` on `execute_tool`
     spans, instrumentation version 5) → Approval with decision `pending`.
  3. MCP `elicitation/create` spans (`mcp.method.name`) → Approval kind `elicitation` (COULD).

  LangGraph interrupt detection is **dropped from REQ-033.c** (Amendment A1). An upstream issue
  is filed against OpenInference to emit interrupt/resume attributes.
- **Consequences:** The graph view still shows LangGraph nodes and retries (DD-011), but not
  interrupts, until upstream support lands.

### DD-011: Agent graph reconstruction (REQ-033.d, REQ-153)

- **Decision:** Graph nodes come from, in priority order:
  1. Spec `invoke_agent` and `invoke_workflow` spans (`gen_ai.agent.name`, `gen_ai.workflow.name`)
     and agent handoffs (`gen_ai.transfer.target.name`).
  2. OpenInference `graph.node.id` / `graph.node.parent_id` / `graph.node.name` (OpenAI Agents
     via OpenInference, Agno workflows).
  3. LangGraph: spans whose JSON `metadata` attribute contains `langgraph_node`, with step =
     `langgraph_step`.

  Edges: the parent node (nearest graph-node ancestor) → child, plus sequence edges between
  consecutive sibling nodes by start time. Execution count per node name counts retries.
  `query.graph` assembles the `AgentGraph` DTO. `components.graph` renders a Mermaid
  `flowchart LR` with `classDef taken|error|approval|retry`, `security_level="strict"`,
  `font_awesome=False`, `icon_packs=[]` (no jsDelivr fetch, REQ-324.b **[spike S1 verifies no
  external request]**), and `on_node_click` → select span.
- **Consequences:** The graph shows the *executed* structure only (as the PRD states). Above 150
  nodes, repeated executions collapse into one node with a count.

### DD-012: Token totals without double counting

- **Decision:** Run, Session and model aggregates sum tokens **from LLMCall rows only**. Agent
  spans carrying aggregated usage are ignored for totals, which covers PydanticAI
  `gen_ai.aggregated_usage.*`, non-LLM spans with `gen_ai.usage.*`, and LangSmith
  `gen_ai.usage.total_tokens`. **Fallback:** if a trace has **no** LLMCall rows but an agent span
  reports usage, the Run uses that agent-level usage, flagged `usage_source="agent"` and shown
  with a footnote in the UI.
- **Consequences:** Correct totals for PydanticAI (which reports both levels) without losing data
  for instrumentations that only report agent-level usage.

### DD-013: Redaction engine (REQ-039)

- **Decision:** Use the `regex` package (Apache-2.0), whose `timeout=` parameter enforces the
  per-value budget (fail closed), compiled once per rule set version.
  - **Built-in presets:**
    - `secrets`: `al_` keys, `sk-…`/`sk-proj-…`, `AKIA…`, GitHub `ghp_`/`github_pat_`, Slack
      `xox[baprs]-`, JWT-shaped strings, `-----BEGIN … PRIVATE KEY-----`
    - `email`: RFC 5322-lite
    - `phone`: E.164 and common national formats
    - `credit_card`: 13–19 digits plus a Luhn check in a callback
  - **Field rules** match attribute keys by `fnmatch` glob against flattened keys
    (e.g. `gen_ai.input.messages`, `llm.input_messages.*`, `*.email`).
  - **What gets redacted:** every string leaf in span, resource and event attributes, status
    messages, exception messages and stack traces, and values inside JSON-encoded string
    attributes (`gen_ai.input.messages` is parsed, redacted leaf by leaf, then re-serialized).
- **Alternatives:** stdlib `re` (no timeout, so catastrophic backtracking could stall ingest);
  `google-re2` (linear time, but no lookarounds or backreferences, which surprises users writing
  rules).
- **Consequences:** One small dependency. Settings validates rules at save time by compiling them
  and running them against a 64 KiB adversarial sample under the timeout.

### DD-014: API key hashing (PRD Q10)

- **Decision:** Secrets have about 190 bits of CSPRNG entropy. Store `sha256(salt‖secret)` with a
  16-byte random per-key salt, and compare with `hmac.compare_digest`. A positive verification
  cache per process holds entries for 5 s and is cleared on the `settings_changed` event.
- **Alternatives:**
  - Argon2id: unnecessary for high-entropy secrets and costs about 50 ms per verification (fails
    REQ-302 at 50 req/s without caching).
  - HMAC with a server pepper: a lost pepper invalidates all keys, and every process must share it.
- **Consequences:** Meets REQ-320.a ("salted"). Verification costs microseconds.

### DD-015: Rate limiting (REQ-007)

In-memory token buckets per (process, Project) for requests and spans, refilled lazily. The
effective cluster limit is N × configured (documented, API §3.5). Retry-After is derived from
bucket deficit/refill, rounded up to ≥ 1 s.

### DD-016: Pricing data (PRD Q9)

- **Decision:** The bundled `data/pricing.csv` is generated by a **dev-time** script
  (`scripts/update_pricing.py`, network allowed only there) from LiteLLM's
  `model_prices_and_context_window.json`, which is MIT-licensed. **Verify the license file at
  each snapshot**; attribution goes in `NOTICE`.
  - Columns: `provider, model_pattern, input_per_mtok, output_per_mtok, cached_input_per_mtok,
    cache_write_per_mtok, source, snapshot_date`.
  - On an empty DB it is loaded as pricing version 1.
  - Release notes state the snapshot date.
- **Consequences:** No runtime fetch (Art. 3). Stale prices are visible as the snapshot date in
  Settings → Pricing.

### DD-017: UI composition and silverpoint mapping (PRD Q6)

- **Decision:** Charts use silverpoint 0.8.0 via `reflex-silverpoint-react` 0.8.1, which is
  self-contained: fonts are bundled and there is no CDN. Event `on_select` gives click-to-filter
  (REQ-141.c). Pages use silverpoint UI/composed components (`sp_table`, `sp_drawer`, tabs,
  inputs, `sp_empty`, `sp_result`) where they fit, and Reflex/Radix primitives otherwise. **Do not
  use `sp_timeline`**: TimelineJS requests an external icon font. Theme: `silverpoint_provider`
  with one palette. Status and kind colors are derived from palette fields and validated with
  `check_palette`.

| PRD chart (REQ-141.b) | silverpoint chart | Gap / note |
|---|---|---|
| KPI cards (106.g) | `kpi_card` | — |
| Runs per bucket by status (106.a) | `stacked_bar_chart` (x = bucket label, keys = statuses) | — |
| Tokens by model (106.b) | `stacked_bar_chart` (x = model; keys = input/output/cached) | — |
| Cost by model (106.b) | `bar_chart` (rows) | — |
| Tokens/cost over time by model (106.b) | `stacked_bar_chart` per bucket | **Gap:** no N-series line/area chart (`line_chart` = 1 series + baseline, `stream_chart` = 2 waves). Upstream issue #1 |
| p50/p95 latency by operation (106.c) | `dumbbell_chart` (start = p50, end = p95) | **Gap:** box plot from precomputed quantiles (`box_plot_chart` bins raw observations). Upstream issue #2 |
| Tool success vs. error (106.d) | `stacked_bar_chart` (rows) | — |
| Top tools (106.d) | `bar_chart` (rows) | — |
| Cost by Source (106.e) | `donut_chart` (≤ 6 Sources, rest grouped as "other"), else `bar_chart` | — |
| Approval outcomes (106.f) | `donut_chart` | — |
| Session cumulative (160.c) | `area_chart` | — |
| Token usage from metrics (107) | `area_chart` / `stacked_bar_chart` | Same gap as issue #1 |
| Time axes | Pre-formatted string bucket labels | **Gap:** no time-scale x axis for series charts. Upstream issue #3 |

  **Trace waterfall and replay timeline** are a span-tree *table* with proportional bars, built
  from Reflex primitives with silverpoint theme tokens. They are not charts. This is a deliberate
  interpretation of Constitution Art. 7, recorded in the Constitution check. Reasons: they need
  nested collapse, per-row selection and thousands of rows, and silverpoint's `interval_chart`
  (lanes, hand-drawn) does not fit that. Upstream issue #4 proposes a "trace waterfall" chart
  for silverpoint.
- **Consequences:** Four upstream issues are filed before v0.1 (PRD §4.1 goal).

### DD-018: CSP (REQ-193, PRD API §7.4 open item)

- **Finding:** Reflex 0.10 compiles to React Router 8 + Vite. The document head contains inline
  scripts (theme preload, `<Scripts/>`, `<ScrollRestoration/>`) and **no nonce support**.
  Emotion injects `<style>` tags.
- **Decision:**
  - `script-src 'self' 'sha256-…'`: hashes computed by the build hook from the inline scripts
    of **every exported `*.html`** (index, 404, SPA fallback) and shipped as `_frontend/csp.json`.
    S1: the served HTML is byte-identical across requests, so hashing works and the fallback below
    is not needed; but two exports of the same source produce different inline scripts
    (content-hashed chunk names, non-reproducible bundle), so hashes must always come from the
    shipped artifact, never from a list kept in the source tree.
  - `style-src 'self' 'unsafe-inline'`.
  - `connect-src 'self'` plus an explicit same-origin `ws(s)://<host>` derived per request.
  - If S1 shows inline scripts vary per request (not hashable), fall back to
    `script-src 'self' 'unsafe-inline'`, recorded as a known limitation.

  Embedded hosts receive the documented list.
- **Consequences:** `unsafe-eval` stays forbidden. The XSS defense in depth relies primarily on
  inert rendering (REQ-327): React escaping, sanitized Markdown with raw HTML disabled, and
  Mermaid `strict`.

### DD-019: Configuration loading

`pydantic-settings` models per section, with a custom source chain: defaults → TOML (`tomllib`)
→ env (`AGENTLENS_<SECTION>_<KEY>`, plus a `_FILE` resolver) → CLI overrides. Unknown keys are
detected by diffing parsed TOML keys and `AGENTLENS_*` environment names against the model
(REQ-202.b). Loading is pure (no I/O beyond reading the files) and happens in `configure()` /
the CLI, never at import.

### DD-020: Logging and self-telemetry (REQ-323, 341, 342)

- **Logging:** stdlib `logging` plus a JSON formatter in `core.logging`. A **content guard
  filter** keeps only allowlisted `extra` fields (IDs, counts, sizes, durations, keys, error
  classes) and replaces exception messages with the exception class name for exceptions raised
  inside `ingest.normalize`/`ingest.redaction`. Full tracebacks are emitted only at `DEBUG` and
  without the local variables (no `rich` locals).
- **Self-telemetry:** extra `reflex-agentlens[selftelemetry]` (`opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http`/`-grpc` 1.45.x).
  - Spans: `agentlens.ingest.request`, `agentlens.write.group`, `agentlens.query.<name>`.
  - Metrics: `agentlens.ingest.spans{outcome}`, `agentlens.ingest.queue.bytes`,
    `agentlens.write.commit.duration`, `agentlens.query.duration{query}`.
  - **Loop check:** resolve the endpoint host. If it is loopback, unspecified or one of this
    process's bind addresses, and the port equals our HTTP or gRPC port, exit 2. Exported spans
    carry `service.name=reflex-agentlens` and no attribute values from ingested data.

### DD-021: Container image and compose topologies

- **Decision:** A multi-stage `Dockerfile`:
  1. `builder`: `ghcr.io/astral-sh/uv` + Python 3.13 + Bun 1.4.4. Runs `uv sync --frozen
     --no-dev --no-editable`, which runs the build hook and produces the pre-built frontend
     inside the installed package.
  2. `runtime`: `python:3.13-slim`, venv copied from the builder, UID/GID 10001, `/data` volume,
     no Node/Bun/compilers. `HEALTHCHECK` uses `python -c` (urllib on `/healthz`).
     `ENTRYPOINT ["reflex-agentlens"]`, `CMD ["serve"]`, `ENV AGENTLENS_SERVER_HTTP_HOST=0.0.0.0`.

  Multi-arch (`linux/amd64`, `linux/arm64`) via buildx in CI.

**Topologies**

```
T1 — uvx (laptop)
  uvx reflex-agentlens ──▶ [ serve: UI + OTLP/HTTP :8000 + gRPC :4317 ] ── SQLite ($XDG_DATA_HOME)

T2 — compose.yaml (try it)
  host 127.0.0.1:8000, :4317 ──▶ [ container: serve ] ── SQLite on volume agentlens-data:/data
  (auth.mode=none set explicitly; ports bound to loopback only)

T3 — compose.postgres.yaml (production)
                    ┌──────────────── internal network ────────────────┐
  :443  ──▶ Caddy ──┼──/v1/* ─────────▶ collector ×N  (collect :4318)  │
  :4317 ──▶ Caddy ──┼──gRPC (h2c) ────▶ collector ×N  (collect :4317)  │──▶ PostgreSQL 18
  :443  ──▶ Caddy ──┼──/* + ws ───────▶ app (serve, ingest_enabled=false)│    (volume)
                    └───────────────────────────────────────────────────┘
  app holds LISTEN agentlens_changes; collectors NOTIFY on commit.
  Only Caddy publishes ports. TLS: Caddy automatic HTTPS (public domain) or `tls internal`.
```

- **Consequences:** `serve` in T3 still runs finalizer and purge jobs, gated by PG advisory locks
  (`job_lock`), so any process can own them. Migrations run in `app` (`auto_migrate`) under an
  advisory lock, and collectors wait for `schema_status == CURRENT` before admitting traffic
  (`/readyz` stays 503 until then).

### DD-022: Repository and packaging layout

- One repository managed as a **uv workspace**:
  - root package `reflex-agentlens` (`src/` layout, hatchling with a custom build hook)
  - member `site/` (`agentlens-site`, not published)
  - member `examples/` (bundled example agents for the dataset generator, not published)

  `uv.lock` is committed. Everything uses `uv`: `uv sync`, `uv run pytest`, `uv run mypy`,
  `uv build`, `uv publish` (trusted publishing), and `uvx` for one-off tools.
- Dependency groups: `dev` (ruff, mypy, pytest, pytest-asyncio, hypothesis, import-linter,
  coverage, playwright), `docs` (mkdocs-material), `bench`.

### DD-023: Project website hosting

- **Decision (partial):** `site/` is a Reflex app that **embeds agentlens pages via `mount(...,
  demo=True, pages={...})`**. That is the second consumer of the embedding API (REQ-231.a) and
  doubles as the embedded smoke test. It needs a running backend (Reflex state), so it ships as a
  container: the same base as the agentlens image plus the site code and the demo SQLite file,
  generated at build time by loading the sample dataset. MkDocs builds static docs that are
  served alongside or linked.
- **Open:** the target hosting, i.e. the stack used for seraph.to. **Owner: Ernesto. Needed
  before plan phase "Site".** See §10 Q-T1.

---

## 5. Patterns and conventions

### 5.1 Code structure

```
src/reflex_agentlens/
├── __init__.py              # mount, standalone_app, configure, AgentlensConfig, Viewer, AuthHook, pages (lazy)
├── core/                    # leaf: no imports from other agentlens packages
│   ├── config.py  errors.py  ids.py  timeutil.py  money.py  logging.py  security.py
│   └── semconv/  constants.py  mapping.py  dialects.py  messages.py   # message/tool-I/O normalizer, used by ingest AND query (Data Model N1)
├── ingest/                  # no reflex, no query
│   ├── engine.py            # thread + loop, supervisor, submit()
│   ├── http.py  grpc.py  admission.py  limits.py  decode.py  validate.py
│   ├── normalize/  spans.py  classify.py  llm.py  tool.py  approval.py  graph.py  messages.py  search.py
│   ├── redaction/  presets.py  rules.py  engine.py  preview.py  hook.py
│   ├── cost.py  metrics.py  writer.py  jobs.py
├── store/                   # no reflex, no ingest, no query
│   ├── protocol.py  records.py  schema.py  dialect.py  changefeed.py  locks.py
│   ├── sqlite/  backend.py  fts.py
│   ├── postgres/  backend.py  fts.py  notify.py
│   └── migrations/  env.py  versions/
├── query/                   # no reflex; may import ingest.redaction.preview only
│   ├── service.py  admin.py  dto.py  filters.py  urlgrammar.py  graph.py  subscribe.py  cursors.py
├── components/              # reflex: waterfall, drawer, messages, json_view, graph (mermaid text), charts, filters, theme
├── pages/                   # reflex: _base (AgentlensState), dashboard, runs, trace, sessions, session, settings, login
├── app/                     # reflex: mount.py, standalone.py, lifespan.py, auth.py, csp.py, routes.py (Starlette)
├── cli/                     # main.py, banner.py
├── data/                    # pricing.csv, sample_dataset/*.json (OTLP/JSON)
├── _webapp/                 # build-only Reflex app module + rxconfig template (export target)
└── _frontend/               # GENERATED at build (gitignored): build/client/**, csp.json
tests/ unit/ golden/ property/ contract/ integration/ smoke/ docker/ security/ bench/ fixtures/
examples/ (uv workspace member) pydantic_ai_agent/ langgraph_agent/ fake_llm/
site/ (uv workspace member)   docs/ (MkDocs)   scripts/ (generate_dataset.py, update_pricing.py, check_req_coverage.py)
Dockerfile  compose.yaml  compose.postgres.yaml  Caddyfile  .github/workflows/  hatch_build.py
```

Import contracts (`import-linter`, in `pyproject.toml`):

1. **Layers:** `pages | components | app | cli` > `query` > `store` and `ingest` > `core`.
2. **Forbidden:**
   - `ingest`, `store`, `query`, `core` → `reflex`, `reflex_silverpoint_react`, `reflex_mermaidjs`
   - `ingest` → `query`
   - `store` → `ingest`
   - `query` → `ingest` (exception: `ingest.redaction.preview`)
   - `pages`/`components` → `store` (they must go through `query`)

### 5.2 Patterns

| Pattern | Where | Why |
|---|---|---|
| Ports & adapters | `store.protocol` + backends | Contract suite, v0.2 ClickHouse backend (REQ-070) |
| Pipeline of pure functions | `ingest.normalize`, `ingest.redaction`, `ingest.cost` | Golden and property tests. No I/O inside the normalizer |
| Single-writer / group commit | `ingest.writer` | Throughput and durability on SQLite (DD-003) |
| Recompute-from-facts | Run and Session derivation (DD-004) | Order independence by construction |
| Table-driven mapping | `core.semconv.mapping` | Art. 1. Dialects without code paths |
| Application service | `query.service`, `query.admin` | Authorization in one place (REQ-174, 191.c) |
| Lazy public API | `reflex_agentlens.__init__` (`__getattr__`) | No import-time I/O or heavy imports (REQ-190.c) |

### 5.3 Semconv mapping table (initial content; normative for `MAPPING_VERSION = 1`)

Notation: sources in priority order; the first present wins. `json(attr, path)` reads inside a
JSON-string attribute. `flat(prefix)` reassembles OpenInference flattened lists.

**Span classification** (first match wins): (1) `gen_ai.operation.name` → `chat`,
`text_completion`, `generate_content`, `embeddings` = **llm**; `execute_tool` = **tool**;
`invoke_agent`, `create_agent`, `invoke_workflow` = **agent**; `retrieval`, `plan`, `*_memory*`
= **generic GenAI** (`is_genai`). (2) `openinference.span.kind` → `LLM`, `EMBEDDING` = llm;
`TOOL` = tool; `AGENT` = agent; `CHAIN` with `json(metadata, $.langgraph_node)` = agent (graph
node); others = generic GenAI. (3) `langsmith.span.kind` → `llm` / `tool` / `chain`. (4) An
approval rule matches (§5.4) = **approval**. (5) Otherwise **generic** (non-GenAI unless another
`gen_ai.*`/mapped key is present).

| Target field | Sources (priority) |
|---|---|
| operation | `gen_ai.operation.name` · derived from `openinference.span.kind` · `langsmith.span.kind` |
| provider | `gen_ai.provider.name` · `gen_ai.system` · `llm.provider` · `llm.system` |
| request_model | `gen_ai.request.model` · `llm.model_name` · `langsmith.metadata.ls_model_name` |
| response_model | `gen_ai.response.model` · `llm.model_name` |
| input_tokens | `gen_ai.usage.input_tokens` · `gen_ai.usage.prompt_tokens` · `llm.token_count.prompt` |
| output_tokens | `gen_ai.usage.output_tokens` · `gen_ai.usage.completion_tokens` · `llm.token_count.completion` |
| cached_input_tokens | `gen_ai.usage.cache_read.input_tokens` · `llm.token_count.prompt_details.cache_read` |
| cache_write_tokens | `gen_ai.usage.cache_write.input_tokens` · `gen_ai.usage.cache_creation.input_tokens` · `llm.token_count.prompt_details.cache_write` |
| reasoning_tokens (stored, not priced separately) | `gen_ai.usage.reasoning.output_tokens` |
| finish_reasons | `gen_ai.response.finish_reasons` · per-message `finish_reason` (deprecated) |
| params | `gen_ai.request.{temperature,max_tokens,top_p,top_k,frequency_penalty,presence_penalty,seed,stream}` · `json(llm.invocation_parameters, $.*)` · `json(model_request_parameters, $.*)` |
| input messages | `gen_ai.input.messages` (+ `gen_ai.system_instructions`) · events `gen_ai.system.message`/`gen_ai.user.message`/`gen_ai.assistant.message`/`gen_ai.tool.message` · event `gen_ai.client.inference.operation.details` · `flat(llm.input_messages)` · `gen_ai.prompt` |
| output messages | `gen_ai.output.messages` · event `gen_ai.choice` · `flat(llm.output_messages)` · `gen_ai.completion` |
| tool_name | `gen_ai.tool.name` · `tool.name` |
| tool_call_id | `gen_ai.tool.call.id` · `tool_call.id` |
| tool_type | `gen_ai.tool.type` |
| tool_args | `gen_ai.tool.call.arguments` · `input.value` (on TOOL spans) |
| tool_result | `gen_ai.tool.call.result` · `output.value` (on TOOL spans) |
| agent_name | `gen_ai.agent.name` · `agent_name` (PydanticAI) · `agent.name` · `graph.node.name` |
| session external id | `gen_ai.conversation.id` · `session.id` · `langsmith.metadata.thread_id` · `langsmith.metadata.session_id` · `langsmith.metadata.conversation_id` |
| graph node | (agent spans) agent_name/`gen_ai.workflow.name` · `graph.node.id` (+ `graph.node.parent_id`) · `json(metadata, $.langgraph_node)` (+ `$.langgraph_step`) |
| error type | `error.type` · span status ERROR + `exception.type` event attribute |
| content-bearing keys (for REQ-040 drop) | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, `gen_ai.prompt`, `gen_ai.completion`, `llm.input_messages.*`, `llm.output_messages.*`, `input.value`, `output.value`, `pydantic_ai.all_messages`, `final_result`, GenAI message events |
| ignored for totals (DD-012) | `gen_ai.aggregated_usage.*`, `gen_ai.usage.total_tokens`, `gen_ai.usage.details.*` (kept raw) |

Messages are normalized to the API `Message` DTO. The genai part types `text`, `tool_call`,
`tool_call_response` (→ `tool_result`) and `reasoning` map 1:1; `blob`, `uri`, `file`,
`server_tool_call*` and `compaction` go to `other{type, raw_json}`.

**Metrics persisted** (REQ-005, Amendment A2): `gen_ai.client.token.usage` (legacy histogram,
still emitted by PydanticAI and others), `gen_ai.client.inference.usage.*` (counters, new),
`gen_ai.client.operation.duration`, `gen_ai.client.inference.duration`,
`gen_ai.client.inference.time_to_first_chunk`, `gen_ai.client.operation.time_to_first_chunk`
(legacy), `gen_ai.server.*`, `gen_ai.invoke_agent.*`, `gen_ai.execute_tool.duration`. Any
other `gen_ai.*` metric is stored generically. Non-`gen_ai.*` metrics are discarded.

### 5.4 Shared attribute vocabulary for sibling projects (REQ-251; proposed upstream)

| Attribute | Type | Meaning |
|---|---|---|
| `reflex_agentkit.approval.id` | string | Stable approval request ID. Its presence makes the span an **approval** span (start = requested, end = decided) |
| `reflex_agentkit.approval.decision` | enum `approved`\|`rejected`\|`edited`\|`timeout`\|`pending` | Outcome |
| `reflex_agentkit.approval.approver` | string | User identifier (subject to redaction rules) |
| `reflex_agentkit.approval.tool_call.id` | string | Links to the guarded `gen_ai.tool.call.id` |
| `reflex_agentkit.agui.run_id` / `.thread_id` | string | AG-UI identifiers. Use `gen_ai.conversation.id` for the session; `thread_id` is kept for cross-reference only |
| `reflex_agentkit.agui.event_count` | int | AG-UI events streamed in the run |
| `reflex_mcphost.server.name` / `.transport` (`stdio`\|`streamable_http`) | string | MCP server identity. reflex-mcphost SHOULD also emit spec `mcp.*` plus `gen_ai.operation.name=execute_tool` |
| `reflex_mcphost.approval.*` | same shape as `reflex_agentkit.approval.*` | Host-side tool approvals |

Mapping (§5.3) treats `reflex_mcphost.approval.*` identically to `reflex_agentkit.approval.*`.
PydanticAI `pydantic_ai.tool.deferral.name` → approval, decision `pending`, linked by
`gen_ai.tool.call.id`.

### 5.5 Error handling

The domain exceptions are the API §6.6 hierarchy (`core.errors`). They map to HTTP/gRPC
statuses only in `ingest.http`/`ingest.grpc`, and to UI states only in `pages._base`, which
wraps every event handler: `StorageUnavailable` → banner, `Forbidden` → state, anything else →
generic error state plus a log line with the exception class only.

---

## 6. Security

### 6.1 Attack surface

| Vector | Mitigation | REQ |
|---|---|---|
| Unauthenticated writes to ingest | Key checked before the body is read. Revocation ≤ 5 s | 006, 320 |
| Ingest DoS (large, compressed or many requests) | Content-Length precheck, streaming decompression cap, token buckets, bounded queue, 503 backpressure | 007, 009 |
| ReDoS through user rules | `regex` timeout, fail closed, save-time adversarial test | 039.d |
| SQL injection through filters, attribute keys or FTS | SQLAlchemy bound parameters. Attribute keys as JSON-path parameters, validated against `^[A-Za-z0-9_.\-:]{1,256}$`. FTS tokens whitelisted (DD-006) | 325 |
| Stored XSS through prompt/tool content | React text rendering. Markdown with raw HTML disabled plus a link scheme allowlist, no remote images. Mermaid `strict`. Labels escaped in generated Mermaid text. CSP (DD-018) | 327, 193 |
| CSRF on login/logout | Origin/Referer check plus SameSite=Lax. State-changing UI actions travel over the Reflex websocket (no cookie-authenticated GET/POST) | 224, 321 |
| Session or cursor forgery | HMAC-signed cookie and cursors with `session_secret` (generated with 0600 perms) | 321 |
| Reflex CORS default `*` | `cors_allowed_origins` set to `server.public_url` (or same-origin derived), never `*` | 325 (Amendment A4) |
| Proxy header spoofing | `proxy` mode trusts headers only if the peer IP ∈ `trusted_proxies` (Reflex `trusted_proxy_hops=0`) | 224 |
| Exposure on the network by accident | Non-loopback bind without explicit `auth.mode` → exit 2 | 224.b |
| Supply chain | `uv.lock` with hashes, pip-audit (`uvx pip-audit`), gitleaks, trivy image scan, hadolint, pinned GitHub Actions by SHA | 326, 328 |
| Container breakout impact | Non-root 10001. Read-only rootfs compatible (writes only to `/data` and `/tmp`). No shell tools needed at runtime | 326 |
| Third-party asset fetch (privacy leak) | silverpoint bundles its fonts. Mermaid `font_awesome=False`, `icon_packs=[]`. S1 verifies with Playwright request interception | 324 |

### 6.2 Sensitive data

| Data | Classification | Storage | Access |
|---|---|---|---|
| Prompt/completion/tool content | Confidential (may hold PHI/PII) | DB, after redaction. Droppable per Project | Viewers of the Project |
| API keys | Secret | Salted SHA-256 + `key_id`. Plaintext shown once (UI/CLI/TTY or 0600 file) | Admins (metadata only) |
| Admin password, session secret, DB password, bootstrap key | Secret | Env/`*_FILE`. `session_secret` file 0600 | Process only |
| Logs | Internal | stdout JSON. Content-free by the guard filter | Operators |
| Self-telemetry | Internal | External endpoint. Content-free | Operators |

---

## 7. Observability

### 7.1 Logging

```json
{"ts":"2026-10-30T12:00:00.123Z","level":"INFO","logger":"agentlens.ingest","event":"export.accepted",
 "project_id":"01JB…","protocol":"http/protobuf","spans":512,"rejected":0,"duplicates":3,
 "bytes":183442,"duration_ms":61,"request_id":"9f1c…"}
```

Allowlisted extra keys: `project_id, source_id, trace_id, span_id, key_id, protocol, spans,
rejected, duplicates, bytes, duration_ms, queue_bytes, status, error_class, attribute_key,
job, deleted_traces, request_id`. Anything else is dropped by the guard filter (REQ-323).

### 7.2 Metrics (self-telemetry, when enabled)

| Metric | Type | Description |
|---|---|---|
| `agentlens.ingest.requests` | Counter{protocol, outcome} | accepted / rejected_auth / rate_limited / too_large / overloaded / invalid |
| `agentlens.ingest.spans` | Counter{outcome} | accepted / rejected / duplicate / truncated |
| `agentlens.ingest.metrics.discarded` | Counter | Non-GenAI metric points discarded (REQ-005.b) |
| `agentlens.ingest.queue.bytes` | Gauge | WriteQueue occupancy |
| `agentlens.write.commit.duration` | Histogram (s) | Group commit latency |
| `agentlens.write.group.spans` | Histogram | Spans per group |
| `agentlens.query.duration` | Histogram{query} | Query-layer latency |
| `agentlens.redaction.timeouts` | Counter | Fail-closed replacements |
| `agentlens.changefeed.lag` | Histogram (s) | Commit → UI notice |

### 7.3 Alerts

Suggested in the operations docs. Not shipped as config, since agentlens has no alerting in v0.1:

| Alert | Condition | Severity |
|---|---|---|
| Ingest overload | `outcome=overloaded` > 1 % for 5 min | Warning |
| Commit latency | p95 > 2 s for 5 min | Warning |
| Not ready | `/readyz` 503 > 1 min | Critical |
| Redaction timeouts | > 0 sustained | Warning (rule needs review) |

---

## 8. Testing strategy

| Level | Target | Tools | Covers |
|---|---|---|---|
| Unit | ≥ 85 % lines on `ingest`+`store`+`query` (CI gate) | `uv run pytest`, `pytest-asyncio`, `coverage` | Mapping, classification, messages, redaction, cost, URL grammar, cursors, config |
| Golden | 7 recipes + LangSmith variant + edge fixtures | Recorded OTLP/JSON in `tests/fixtures/otlp/` | REQ-260, 033–036 |
| Property | Order, batching and duplicate invariance | `hypothesis` (permutations, splits, duplicates of fixture span sets) | REQ-037.a, 038.a, 365 |
| Contract | `StorageBackend` on SQLite and PG 15/18 | pytest parametrized backend fixture. PG via GitHub service container (`AGENTLENS_TEST_PG_URL`) | REQ-070.b, 072, 073.b, multi-writer |
| Integration | Real HTTP/gRPC endpoints, auth, limits, backpressure, kill test, DB outage | `httpx`, `grpcio` client, subprocess `serve` | REQ-001–010, 009.a, 330, 331 |
| Security | Canary scans (DB bytes, logs), injection cases, XSS fixture, outbound socket interception, key-plaintext scan | pytest + Playwright | REQ-320, 322–327 |
| Smoke | Standalone, embedded host (state-name collisions), site. No console errors. No third-party requests. CSP header | Playwright (Chromium) | REQ-190.c/d, 193, 363 |
| Docker | Build, healthy, 60 s first-Run test, compose.postgres to healthy | `docker buildx`, compose | REQ-203–205, 210.a, 364 |
| Traceability | Every MUST criterion cited | `scripts/check_req_coverage.py` (parses PRD IDs, greps tests) | REQ-362 |
| Benchmark | All §7.1 targets on the reference machine | `tests/bench/` load driver (async httpx + protobuf), dataset generator, query timer. JSON report | REQ-366 (not a PR gate) |

CI (GitHub Actions, `astral-sh/setup-uv`, actions pinned by SHA):
- `lint` (ruff check/format, import-linter)
- `types` (`mypy --strict` on core layers)
- `test` (3.11/3.12/3.13 × SQLite; 3.13 × PG 15 and 18)
- `frontend-build` (Bun, `uv build`, then a wheel boot test with no node on PATH)
- `smoke` (Playwright)
- `docker` (buildx, healthcheck, 60 s test, compose.postgres)
- `security` (gitleaks, `uvx pip-audit`, hadolint, trivy)
- `docs` (mkdocs build `--strict`)
- `release` on `v*` tags: PyPI trusted publishing, GHCR push of multi-arch tags

---

## 9. Rollout and compatibility

### 9.1 Release strategy
- `0.1.0a*` pre-releases (PyPI + GHCR `:0.1.0-alpha.N`) once Phase "Core + UI" passes. The site
  demo runs the alpha. `0.1.0` when every MUST passes and the benchmark report meets §7.1.
- No feature flags. Optional capabilities are configuration (gRPC, live updates, self-telemetry).

### 9.2 Compatibility
- Schema: Alembic migrations only, forward-only within 0.x. `db status` reports drift.
- Mapping: `MAPPING_VERSION` per span. A mapping bump documents "run `reflex-agentlens rebuild`".
- Reflex: pinned `>=0.10.1,<0.11` because of the internal env vars (DD-002). Reflex 0.9 and 0.10
  state stores are incompatible, which is irrelevant here because state lives in
  `{data_dir}/states` per version.

### 9.3 Pinned runtime dependencies (initial; exact versions locked in `uv.lock`)

| Package | Constraint | Verified current (2026-10-30) | License |
|---|---|---|---|
| `reflex` | `>=0.10.1,<0.11` | 0.10.1 | Apache-2.0 |
| `reflex-silverpoint-react` | `>=0.8.1,<0.9` | 0.8.1 (wraps `@silverpoint/react` 0.8.0) | MIT |
| `reflex-mermaidjs` | `>=0.2.1,<0.3` | 0.2.1 (mermaid 12.0.0) | MIT |
| `opentelemetry-proto` | `>=1.45.2,<2` | 1.45.2 | Apache-2.0 |
| `grpcio` | `>=1.85.1,<2` | 1.85.1 | Apache-2.0 |
| `sqlalchemy[asyncio]` | `>=2.0,<2.1` (lower bound from lock) | verify at lock time | MIT |
| `alembic` | `>=1.20.1,<2` | 1.20.1 | MIT |
| `aiosqlite` | `>=0.22.1` | 0.22.1 | MIT |
| `asyncpg` | `>=0.32.0` | 0.32.0 | Apache-2.0 |
| `pydantic` / `pydantic-settings` | `>=2` | verify at lock time | MIT |
| `regex` | `>=2024` | verify at lock time | Apache-2.0 |
| `granian` | as required by Reflex **[spike S1]** | — | BSD-3 |
| *extra* `selftelemetry`: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-{http,grpc}` | `>=1.45.2,<2` | 1.45.2 | Apache-2.0 |

Starlette comes through Reflex (`starlette>=1.3.1`). **FastAPI is not used.** Reflex 0.10
dropped it, and agentlens routes are plain Starlette (Amendment A3).

---

## 10. Open questions

- [ ] **Q-T1 (new):** Target hosting for `site/` (the seraph.to stack: provider, container vs.
  PaaS, domain). *Owner: Ernesto. Needed before plan phase "Site".*
- [ ] **Q-T2:** Exact `SEMCONV_GENAI_COMMIT` SHA to pin. *Recorded by the mapping task.* If the
  genai repo cuts a release before v0.1.0, pin the release tag instead.
- [x] **Q-T3 (resolved 2026-10-30: OpenInference, as proposed):** OpenAI Agents SDK recipe instrumentation: **OpenInference** (active, 2.7.0;
  recommended) vs. OTel contrib `openai-agents-v2` (spec `gen_ai.*`, but 0.1.0, last released
  2025-10). The proposal is OpenInference as the recipe, with the contrib package listed as an
  alternative and covered by an extra golden test (SHOULD). *Owner: Ernesto.*
- [ ] **Q-T4:** MkDocs Material is a fixed decision. Its upstream maintenance status is to be
  re-checked at the docs phase. No change is proposed.

## 11. Spikes (run first in the Implementation Plan, results appended here)

| Spike | Question | Exit criterion | Blocks |
|---|---|---|---|
| **S1 — Pre-built Reflex boot** | Does the DD-002 recipe boot `standalone_app()` from an installed wheel with no node/bun on PATH, under Granian, with grpc.aio on a separate thread? Is `env.json` runtime-fetched (port rewrite possible)? Are inline scripts hashable (CSP)? Do Pydantic DTOs work as state vars? Do Mermaid (`font_awesome=False`, `icon_packs=[]`) and silverpoint make zero third-party requests? | A Playwright smoke run passes with CSP enforced and zero external requests. A written report with the decisions for DD-002 consequence 1 and DD-018 fallback | UI phase, Docker, REQ-200.b/210 |
| **S2 — SQL performance** | Do plain SQL write path (DD-003/004) and dashboard aggregates (DD-005) meet REQ-300/303/305 on generated 100k/1M datasets? | Benchmark report. If REQ-305 misses on SQLite, rollups (ladder step 1) are adopted in the Data Model | Data Model finalization of rollup tables, Phase 1 exit |

Spike code and raw results live on the throwaway branches `spike/s1-prebuilt` (`spikes/s1/NOTES.md`)
and `spike/s2-sql` (`spikes/s2/NOTES.md`, `spikes/s2/results/`). They are never merged.

**Machine (all numbers):** AMD Ryzen AI 9 HX PRO 370 (24 threads), 45 GB RAM, NVMe, Linux 7.0,
Python 3.13.9, Reflex 0.10.1, Granian 2.8.7, SQLite 3.51.0, PostgreSQL 18.6 (Docker, default
configuration, loopback TCP), SQLAlchemy 2.0 async (aiosqlite / asyncpg), Chromium via Playwright 1.64.1.

### 11.1 S1 results (2026-10-31): PASSED

| Check | Result |
|---|---|
| Wheel boots with no `node`/`bun` on PATH (`env -i PATH=/usr/bin:/bin`) | Yes. Workdir prepared in 0.03–0.08 s; start → first `200` in 6.3 s (Reflex + app import) |
| Export / wheel | `reflex export --frontend-only` ~28 s; 14 MB wheel, 28 MB unpacked bundle (Mermaid icon packs, ZenUML and ELK chunks dominate) |
| Baked backend URL (DD-002 consequence 1) | Compiled into `reflex-env-*.js`. Rewritten per port (plus precompressed siblings): app works on 9123 → **A6 dropped** |
| Pydantic DTOs as state vars | Yes (`list[BaseModel]` in state, `rx.foreach`, event updates). `pydantic>=2` must be a direct dependency (Reflex 0.10 does not depend on it) |
| Ingest thread (DD-001) | Lifespan-started thread with `grpc.aio` + Starlette `POST /v1/traces` via `api_transformer`: 10,000/10,000 spans counted per protocol |
| UI responsiveness during export | Event round-trip p95 67.9 ms idle vs. 67.3 ms at ~34k spans/s (parse-and-count only; re-measure with the real pipeline in P8) |
| SIGTERM during export | Ordered shutdown, exit 0 in 0.03 s, `:http` and `:4317` released, no leftover process |
| CSP (DD-018) | Enforced with 11 inline-script hashes: 0 violations, no `unsafe-eval`, fully interactive. Hashes stable across requests, **not across builds** |
| Third-party requests (REQ-324.b) | 0 foreign requests, 0 console errors over a full session |
| Import side effects (REQ-190.c) | No network/subprocess. Writes `assets/external/…` and `.states/` into the cwd → consequence 4 of DD-002 |
| Other findings | `telemetry_enabled` defaults to `True` (every rxconfig sets it false; test it); SitemapPlugin enabled implicitly (disable); port in use → raw Granian traceback (consequence 5); sdist build needs a JS runtime (decide in P7-01) |

### 11.2 S2 results (2026-10-31): REQ-300/303/304 PASSED, REQ-305 PASSED on PostgreSQL, MISSED on SQLite

| Measure | SQLite | PostgreSQL | Target |
|---|---|---|---|
| Schema (one SQLAlchemy Core MetaData, `with_variant` + `ddl_if`) | creates; FTS5 + `contentless_delete` OK | creates | — |
| 1M-span load through the write path (5,000-span groups) | 56.9 s, 843 MB | 113.9 s, 1.47 GB | — |
| Write path, 4 producers → 1 writer, 120 s (in-process, no HTTP) | **16,002 spans/s**, commit p95 526 ms | **7,493 spans/s**, commit p95 1,327 ms | ≥ 2,000 (REQ-300) / ≥ 5,000 (REQ-301), indicative |
| Order/batch invariance (10k spans, 4 orders incl. 10 % duplicates) | identical | identical, **and equal to SQLite** | REQ-037.a |
| Q1–Q3 run list and filters, 100k, p95 | ≤ 3.2 ms | ≤ 7.0 ms | < 300 ms (REQ-303) |
| Q5 trace of 1,009 spans, p95 | 8.6 ms | 9.6 ms | < 500 ms (REQ-304) |
| FTS parity on identifiers | 18/18 identical **after** DD-006 normalization (5/18 differed before) | | REQ-073.b |
| Dashboard full set (Q8 + Q10a–f), 1M spans, 30 days, p95 | **3,327 ms** ✗ | **724 ms** ✓ | < 1,500 / < 1,000 (REQ-305) |
| — of which Q10c latency p50/p95/p99 (~850k rows ranked) | 2,519 ms | 360 ms | |
| Aggregate value parity across backends | identical (every sum, bucket and percentile) | | DD-005 |

Notes:
- Q7 runs as one `WITH … UPDATE runs … FROM calc` statement with `ROW_NUMBER()` windows on both
  backends; the affected set is `unnest(:pks)` (PG) or `json_each(:pks)` (SQLite). Bind values
  derived by arithmetic are computed in Python (asyncpg cannot type `:a - :b`).
- PG identity values are consumed by `INSERT … ON CONFLICT DO NOTHING`; surrogate `pk`s differ
  between backends. Contract tests compare natural keys only.
- Percentiles: three separate window passes cost 9.1 s on SQLite; one pass (PG
  `percentile_disc(ARRAY[…])`, SQLite one `ROW_NUMBER()` per source stream) cut it to 2.5 s. The
  rank is `ceil(p·N)` **in IEEE double**, as PostgreSQL computes it; SQLite must use the same
  arithmetic for exact parity (Data Model v1.0.1).
- **Open decision (P0-10.1):** SQLite misses REQ-305.a by ~2.2×, and the miss is entirely the
  latency-percentile panel (the rest of the set takes ~0.8 s). DD-005's ladder step 1 (hourly
  rollups with log2 histograms) would make percentiles approximate, which the PRD allows only via a
  Delta Spec. The options are listed in the S2 report for the maintainer.

---

## 12. Proposed amendments to approved artifacts (apply on approval of this document)

These follow from research findings. They are recorded here for traceability and applied as
PRD v1.1 / API v1.1 edits in the same commit that approves this design.

| ID | Artifact | Change | Reason |
|---|---|---|---|
| A1 | PRD REQ-033.c | Replace "LangGraph interrupt/resume spans" with "PydanticAI deferred tool calls (`pydantic_ai.tool.deferral.*`) as `pending` approvals; MCP `elicitation/create` (COULD)". Add to §11 risks: "LangGraph interrupts not observable via OpenInference until upstream support; upstream issue filed" | DD-010: no attribute exists |
| A2 | PRD REQ-005.a, REQ-107.a | Name both the legacy `gen_ai.client.token.usage` and the new `gen_ai.client.inference.usage.*` counters. REQ-107 reads whichever is present (new preferred) | Spec replaced the metric. Frameworks still emit the old one |
| A3 | PRD §8, API §7.1/§1 | "FastAPI backend/routes" → "Starlette (Reflex backend)" | Reflex 0.10 has no FastAPI dependency |
| A4 | API §8.2 | Add `[server] public_url` (URL; used for the banner, CORS, CSP and the frontend `env.json` rewrite) and `[server] ingest_enabled` (bool, default `true`; `false` for `app` in compose.postgres) | DD-002, DD-021, Reflex CORS default `*` |
| A5 | API §7.4 | `script-src 'self' <build-time sha256 hashes>`, `style-src 'self' 'unsafe-inline'`, `connect-src 'self' ws(s)://<host>`. Fallback to `'unsafe-inline'` for scripts only per S1 result | DD-018 |
| A6 | PRD §7.4 (conditional on S1) — **dropped (S1: the compiled env module is rewritten per port)** | Known limitation "standalone HTTP port fixed at 8000 unless behind HTTPS/reverse proxy", only if `env.json` cannot be rewritten | DD-002 |
| A7 | PRD REQ-250.a | LangGraph recipe = OpenInference (primary) plus LangSmith OTel (secondary, golden test SHOULD). OpenAI Agents SDK recipe per Q-T3 | Research §4 |

---

## Constitution check

| Article | How this design complies |
|---|---|
| Art. 1 | OTLP-only receivers. Pinned target model (genai commit plus core v1.44.0). One declarative mapping table for five dialects (DD-009, §5.3). Raw attributes kept. No framework code paths |
| Art. 2 | SQLite default. No DuckDB, Redis or broker (DD-005, DD-007). Live updates through in-process fan-out plus PG LISTEN/NOTIFY, which is part of the existing database. Rollup fallback stays SQL |
| Art. 3 | Redaction and content drop in the ingest engine before the writer (Flow 1 step 9). Content guard log filter (DD-020). No runtime pricing fetch (DD-016). Zero third-party assets (DD-017, S1) |
| Art. 4 | Key check before body read, salted hashes (DD-014), limits and backpressure (DD-003, DD-015), CSP with hashes (DD-018), non-root image (DD-021), CORS pinned (A4) |
| Art. 5 | ns timestamps. `Decimal`/`NUMERIC`. Unpriced as NULL plus flag. Pricing version per cost. Idempotent insert-or-ignore plus recompute-from-facts (DD-004). Durable group commit (DD-003) |
| Art. 6 | Import contracts (§5.1). Aggregates and percentiles in SQL (DD-005). UI-free core |
| Art. 7 | Pages via `mount()` with lazy imports. Silverpoint for all charts, mermaidjs for the graph (DD-011, DD-017). **Interpretation (not exception):** the trace waterfall/replay timeline is treated as a tabular tree component, not a chart. Upstream issue #4 filed |
| Art. 8 | Test levels mapped to REQs (§8). Property and contract suites. Traceability script. Benchmarks reproducible |
| Art. 9 | `/healthz` and `/readyz`. JSON logs. Self-telemetry as an optional extra with a loop check (DD-020) |
| Art. 10 | Amendments to approved artifacts listed explicitly (§12). Spikes precede dependent work |

**Exceptions requested:** none. **Interpretations requiring your confirmation:** Art. 7
(waterfall as a table), Art. 3 (first-run TTY banner, already accepted with the PRD).

---

## Change history

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0-draft.1 | 2026-10-30 | Ernesto Crespo (with Claude) | Initial draft, based on research of Reflex 0.10.1, GenAI semconv (genai repo / core v1.44.0), silverpoint 0.8.0, reflex-mermaidjs 0.2.1 |
| 1.1 | 2026-10-30 | Ernesto Crespo | Data Model notes: N1 (messages normalizer in `core.semconv.messages`), N2 (SQLite ≥ 3.43), N4 (`storage_usage` returns estimated and physical bytes) |
| 1.0 | 2026-10-30 | Ernesto Crespo | Approved. Amendments A1–A5, A7 applied (PRD v1.1, API v1.1, Constitution v1.1). A6 pending S1. Art. 7 interpretation confirmed |
| 1.1.1 | 2026-10-31 | Ernesto Crespo (with Claude) | Analyze A-03: DD-005 PostgreSQL percentiles use `percentile_disc` (nearest-rank), matching Data Model Q8 |
| 1.2 | 2026-10-31 | Ernesto Crespo (with Claude) | Spike results §11.1 (S1) and §11.2 (S2). DD-001 lifespan-started ingest thread. DD-002 consequence 1 resolved (env module rewrite), consequences 4–5 added; A6 dropped. DD-006 search normalization (A-15). DD-018 hashes over all exported HTML, from the shipped artifact. REQ-305 decision open (P0-10.1) |
| 1.3 | 2026-10-31 | Ernesto Crespo (with Claude) | Analyze A-01: DD-004 concurrency rule (lock Runs then Sessions in a deterministic order; `ON CONFLICT DO UPDATE` stubs; one retry on deadlock) |
