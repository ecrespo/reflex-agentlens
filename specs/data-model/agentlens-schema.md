# reflex-agentlens — Data Model Specification

## Metadata

| Field | Value |
|---|---|
| **Author** | Ernesto Crespo (`ecrespo`) |
| **Status** | `APPROVED` |
| **Version** | 1.3 |
| **Date** | 2026-10-31 |
| **Databases** | SQLite ≥ 3.43 (FTS5, JSON1, window functions, contentless-delete FTS) · PostgreSQL ≥ 15 (CI: 15, 18) |
| **Related Tech Design** | [`specs/technical/agentlens-architecture.md`](../technical/agentlens-architecture.md) v1.0 |
| **Related API Spec** | [`specs/api/agentlens-api-v1.md`](../api/agentlens-api-v1.md) v1.1 |
| **Related PRD** | [`specs/prd/agentlens-v0.1.md`](../prd/agentlens-v0.1.md) v1.1 |
| **Governed by** | [`specs/constitution.md`](../constitution.md) v1.1 |

Tables are defined with SQLAlchemy Core in `reflex_agentlens.store.schema` (Tech Design DD-008).
They are created and evolved only by Alembic migrations in `store/migrations/versions/`.
Revision `0001_initial` creates everything in this document except the conditional §2.19.
The column types below are logical types. §2.0 gives the per-backend physical mapping.

---

## 1. Overview

### 1.1 Entities and access patterns

| Group | Tables | Write pattern | Read pattern |
|---|---|---|---|
| **Settings** | `projects`, `api_keys`, `redaction_rule_sets`, `pricing_versions`, `pricing_entries`, `saved_filters`, `app_meta` | Rare, through `AdminService` | Cached by the ingest engine. Settings page |
| **Facts** | `spans`, `resources`, `metric_points` | Append-only, insert-or-ignore, group commits at 2k–5k spans/s | Trace detail by run. Raw attributes lazily |
| **Per-span derived** | `llm_calls`, `tool_calls`, `approvals`, `graph_nodes`, `span_search` | Inserted with their span, in the same transaction, never updated (except cost recompute) | Aggregates, filters, search |
| **Per-trace / per-session derived** | `runs`, `sessions`, `sources` | Recomputed from facts in each commit (DD-004) | Run list, KPIs, sessions |
| **Operations** | `purge_log` | One row per purge run | Settings → Retention |

**Core rule (Constitution Art. 5, Tech Design DD-004):** the rows in `runs` and `sessions` are a
pure function of the stored `spans` plus per-span derived rows (and the clock, only for the
`in_progress`/`incomplete` distinction). Any of them can be deleted and rebuilt by
`reflex-agentlens rebuild`. Per-span derived rows are a pure function of their span, the mapping
version, and the pricing version.

### 1.2 Relationship diagram

```
projects 1─┬─N api_keys
           ├─1 redaction_rule_sets
           ├─N saved_filters
           ├─N sources
           ├─N resources
           ├─N sessions 1───N runs
           ├─N runs ─────────┬─1 sources (primary)
           │                 └─N spans ──┬─0..1 llm_calls ─── N:1 pricing_versions
           │                             ├─0..1 tool_calls
           │                             ├─0..1 approvals
           │                             ├─0..1 graph_nodes
           │                             ├─0..1 span_search
           │                             └─N:1 resources, N:1 sources
           └─N metric_points ─N:1 sources

pricing_versions 1─N pricing_entries      (global, not per Project)
purge_log, app_meta                       (global)
```

### 1.3 Key conventions

| Convention | Rule | Reason |
|---|---|---|
| Internal keys | `pk` = `BIGINT` identity (SQLite `INTEGER PRIMARY KEY`). Child tables reference `*_pk` | Compact indexes on high-volume tables. FTS5 rowid = `spans.pk` |
| Public IDs | ULID strings (`projects.id`, `sessions.id`, `saved_filters.id`) or natural IDs (`trace_id`, `span_id`, `key_id`) | API §2. Internal `pk`s never leave the `store`/`query` boundary |
| OTel IDs | Binary: `trace_id` 16 bytes, `span_id`/`parent_span_id` 8 bytes (`BLOB`/`BYTEA`). Hex in DTOs and URLs | Half the index size of hex text |
| Time | `BIGINT` Unix **nanoseconds**, UTC, suffix `_ns`. Durations `_ns` too. Retention ages as `_s` (seconds) | Art. 5. OTLP native |
| Money | `BIGINT` **nano-USD** (1 nUSD = 10⁻⁹ USD), suffix `_nusd`. Prices: `BIGINT` nano-USD **per million tokens**, suffix `_nusd_per_mtok`. Converted to `Decimal` in Python | Art. 5: fixed-point, never float. Identical exact arithmetic on both backends (SQLite has no exact `NUMERIC`; see §8 note) |
| Enums | Short `TEXT` with `CHECK (col IN (...))` | Readable in SQL. Same on both backends |
| Booleans | `BOOLEAN` (SQLite: `INTEGER` 0/1 with `CHECK`) | — |
| JSON | `JSON` logical: SQLite `TEXT` with `CHECK (json_valid(col))`, PostgreSQL `JSONB` | Raw attributes, rule sets, small arrays |
| Deletes | Hard deletes only. Retention purges whole traces. No soft delete | Privacy: data past retention must really be gone. Audit is not a v0.1 requirement |
| Audit columns | Settings tables carry `created_at_ns`, `updated_at_ns`, `updated_by` (viewer ID). Fact tables carry `received_at_ns` | — |

### 1.4 Attribute JSON encoding (normative)

OTLP `AnyValue` is stored as JSON per attribute key in one flat object:
`{"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1234, ...}`.

| OTLP type | JSON | Note |
|---|---|---|
| `string_value` | string | Post-redaction. Truncated with marker if > `max_attribute_value_bytes` (REQ-041) |
| `bool_value` | `true`/`false` | |
| `int_value` | number (integer) | Python int keeps the full int64 range |
| `double_value` | number | `NaN`/`±Inf` → strings `"NaN"`, `"Infinity"`, `"-Infinity"` |
| `bytes_value` | `{"$b64": "<base64>"}` | |
| `array_value` | array | Recursive |
| `kvlist_value` | object | Recursive. Keys preserved |

Field rules with action `drop` remove the key. `mask` replaces the value with `"[REDACTED:<rule>]"`.
Keys dropped by the content toggle (REQ-040) are removed and listed in
`spans.dropped_keys` (JSON array of key names only).

---

## 2. Tables

### 2.0 Physical type mapping

| Logical | SQLite | PostgreSQL |
|---|---|---|
| `PK` | `INTEGER PRIMARY KEY` (rowid alias) | `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| `BIGINT` / `INT` / `SMALLINT` | `INTEGER` | `BIGINT` / `INTEGER` / `SMALLINT` |
| `TEXT(n)` | `TEXT` + `CHECK (length(col) <= n)` | `VARCHAR(n)` |
| `TEXT` | `TEXT` | `TEXT` |
| `BYTES(n)` | `BLOB` + `CHECK (length(col) = n)` | `BYTEA` + `CHECK (octet_length(col) = n)` |
| `BOOL` | `INTEGER CHECK (col IN (0,1))` | `BOOLEAN` |
| `JSON` | `TEXT CHECK (json_valid(col))` | `JSONB` |

SQLite connection pragmas (every connection): `journal_mode=WAL` (set once), `synchronous=NORMAL`,
`foreign_keys=ON`, `busy_timeout=5000`, `temp_store=MEMORY`, `cache_size=-65536` (64 MiB).
Set at creation: `auto_vacuum=INCREMENTAL`, `page_size=8192`.

---

### 2.1 `app_meta`

**Purpose:** Instance-level key/value facts. **Volume:** < 20 rows.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `key` | `TEXT(64)` PK | no | — | One of: `instance_id` (ULID), `created_at_ns`, `bootstrap_done` (`"1"`), `mapping_version_min` (lowest `MAPPING_VERSION` present in `spans`, maintained by rebuild), `session_key` (32 random bytes, hex; created on first use with insert-or-ignore so every process sharing the database uses the same key; never logged or shown; Analyze A-08) |
| `value` | `TEXT` | no | — | |

Alembic's own `alembic_version` table holds the schema revision (REQ-074).

### 2.2 `projects`

**Purpose:** Grouping of Sources with settings [REQ-170, 173, 006, 007, 040, 090].
**Volume:** 1–100 rows.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | Internal key |
| `id` | `TEXT(26)` | no | ULID | Public ID (API §2) |
| `slug` | `TEXT(40)` | no | — | `^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$` (validated in `AdminService`; `CHECK` on length only) |
| `name` | `TEXT(120)` | no | — | Display name. Delete confirmation compares against it (REQ-170.d) |
| `content_storage` | `BOOL` | no | `true` | REQ-040.b |
| `retention_max_age_s` | `BIGINT` | no | `2592000` (30 d) | `0` = forever. `CHECK (>= 0)` [REQ-090.a] |
| `request_rate` / `request_burst` | `INT` | no | 50 / 100 | `CHECK (> 0)` [REQ-007.a] |
| `span_rate` / `span_burst` | `INT` | no | 20000 / 40000 | `CHECK (> 0)` |
| `settings_version` | `INT` | no | 1 | Incremented on any change to this row, its rule set or its keys. Ingest caches compare it (REQ-006.c) |
| `created_at_ns` / `updated_at_ns` | `BIGINT` | no | now | |
| `updated_by` | `TEXT(128)` | yes | — | Viewer ID |

**Indexes**

| Name | Columns | Type | Justification |
|---|---|---|---|
| `uq_projects_id` | `id` | unique | Lookup by public ID (every DTO) |
| `uq_projects_slug` | `slug` | unique | URL `project=<slug>` resolution (API §6.2) |

### 2.3 `api_keys`

**Purpose:** Ingest credentials, hashed [REQ-006, 170, 320]. **Volume:** ≤ 10 per Project.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `key_id` | `TEXT(8)` | no | random | Public handle, base32 Crockford lowercase (API §3.4) |
| `project_pk` | `BIGINT` FK → `projects.pk` ON DELETE CASCADE | no | — | |
| `label` | `TEXT(120)` | no | `''` | |
| `salt` | `BYTES(16)` | no | CSPRNG | Per-key salt (DD-014) |
| `secret_hash` | `BYTES(32)` | no | — | `sha256(salt ‖ secret_utf8)` |
| `created_at_ns` | `BIGINT` | no | now | |
| `created_by` | `TEXT(128)` | yes | — | Viewer ID, `"cli"`, or `"bootstrap"` |
| `last_used_at_ns` | `BIGINT` | yes | — | Updated at most once per minute per key |
| `revoked_at_ns` | `BIGINT` | yes | — | Non-null = revoked (terminal) |

**Indexes:** `uq_api_keys_key_id (key_id)` unique (verification lookup, every ingest request on a
cache miss); `ix_api_keys_project (project_pk)` (Settings list).
**Invariant:** no column ever holds the plaintext secret. The canary test scans the whole DB
(REQ-320.a).

### 2.4 `redaction_rule_sets`

**Purpose:** Per-Project redaction configuration [REQ-039, 172]. **Volume:** 1 row per Project.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `project_pk` | `BIGINT` PK, FK → `projects.pk` ON DELETE CASCADE | no | — | |
| `presets` | `JSON` | no | `{"secrets":true,"email":false,"phone":false,"credit_card":false}` | |
| `pattern_rules` | `JSON` | no | `[]` | `[{"name":"…","regex":"…","enabled":true}]`. Max 50 rules, regex ≤ 2 KiB each (validated in `AdminService`) |
| `field_rules` | `JSON` | no | `[]` | `[{"key_glob":"…","action":"drop"\|"mask"}]`. Max 100 |
| `updated_at_ns` | `BIGINT` | no | now | Shown in Settings ("applies to data ingested after …", REQ-039.f) |
| `updated_by` | `TEXT(128)` | yes | — | |

Saving bumps `projects.settings_version` in the same transaction.

### 2.5 `pricing_versions` and `pricing_entries`

**Purpose:** Versioned global pricing [REQ-060, 061, 171]. **Volume:** tens of versions × ~500 entries.

`pricing_versions`

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `version` | `INT` PK | no | max+1 | Monotonic |
| `effective_from_ns` | `BIGINT` | no | now | Version 1 (bundled seed): `0` |
| `source` | `TEXT(16)` | no | — | `CHECK IN ('bundled','editor','csv')` |
| `note` | `TEXT(500)` | yes | — | For the bundled seed: snapshot date and upstream source |
| `created_at_ns` | `BIGINT` | no | now | |
| `created_by` | `TEXT(128)` | yes | — | |

`pricing_entries`

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `version` | `INT` FK → `pricing_versions.version` ON DELETE RESTRICT | no | — | Versions are never deleted |
| `provider` | `TEXT(64)` | no | `''` | `''` = any provider |
| `model_pattern` | `TEXT(200)` | no | — | Exact name, or a glob with `*` |
| `match_kind` | `TEXT(8)` | no | — | `CHECK IN ('exact','glob')`. A prefix match is written as the glob `prefix*` |
| `input_nusd_per_mtok` | `BIGINT` | no | — | `CHECK (>= 0)`. $2.50/Mtok = `2500000000` |
| `output_nusd_per_mtok` | `BIGINT` | no | — | `CHECK (>= 0)` |
| `cached_input_nusd_per_mtok` | `BIGINT` | yes | — | NULL = charged at the input price |
| `cache_write_nusd_per_mtok` | `BIGINT` | yes | — | COULD (REQ-060.d). NULL = charged at the input price |

**Indexes:** `uq_pricing_entry (version, provider, model_pattern)` unique.

**Resolution (normative):** for an LLMCall at time *t*, take the version with
`max(version) WHERE effective_from_ns <= t`. Within it, apply REQ-061.b: try the response model,
then the request model. For each, an `exact` match (provider equal, or `''`) wins over a `glob`
match, and among globs the longest literal prefix wins. Provider-specific entries beat `''`.
The ingest engine holds the current versions in memory (refreshed on `settings_changed`) and
resolves without SQL.

**Cost formula (nano-USD, integers)**, with round-half-even at the final division:

```
non_cached = input − cached           (if the source reports cached as a subset of input; all mapped dialects do)
cost_nusd  = round((non_cached × p_in + cached × coalesce(p_cached, p_in)
                    + output × p_out + cache_write × coalesce(p_cw, p_in)) / 1_000_000)
```

`reasoning_tokens` are already included in `output_tokens` by every mapped dialect, so they are
not priced separately. If input or output tokens are missing, or no entry matches: `cost_nusd`
is NULL and `unpriced = true` (REQ-061.c).

### 2.6 `sources`

**Purpose:** Instrumented apps per Project [REQ-031]. **Volume:** ≤ 1,000 per Project.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `project_pk` | `BIGINT` FK CASCADE | no | — | |
| `service_namespace` | `TEXT(128)` | no | `''` | `''` when absent (keeps the unique index portable) |
| `service_name` | `TEXT(256)` | no | — | `unknown_service` when absent (REQ-031.b) |
| `first_seen_ns` / `last_seen_ns` | `BIGINT` | no | — | `last_seen_ns` is updated at most once per group commit |

**Indexes:** `uq_sources (project_pk, service_namespace, service_name)` unique (upsert by natural
key, API §5.1 step 1; Source selector list).

### 2.7 `resources`

**Purpose:** Deduplicated OTel resource attribute sets [REQ-030.a, 101.f]. **Volume:** about
1–10 per Source and deployment version.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `project_pk` | `BIGINT` FK CASCADE | no | — | |
| `attrs_hash` | `BYTES(16)` | no | — | BLAKE2b-128 of the canonical JSON (sorted keys, post-redaction) |
| `attributes` | `JSON` | no | — | Resource attributes (§1.4) |
| `schema_url` | `TEXT(512)` | yes | — | |

**Indexes:** `uq_resources (project_pk, attrs_hash)` unique (insert-or-ignore per batch).
Orphaned resources (no spans left after purge) are deleted by the purge job.

### 2.8 `sessions`

**Purpose:** Conversation grouping [REQ-036, 105, 160]. **Volume:** ~0.2 × runs.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `id` | `TEXT(26)` | no | ULID | Public ID (routes `/sessions/[session_id]`) |
| `project_pk` | `BIGINT` FK CASCADE | no | — | |
| `external_id` | `TEXT(512)` | no | — | Conversation ID as sent. If redaction changes it, the stored value is `<redacted value> + '~' + hex(HMAC-SHA256(app_meta.session_key, raw ID))[:16]`, so unrelated conversations stay apart and the raw ID is never stored (Analyze A-08). Longer values truncated and hashed: `trunc(480) + '…' + hex(blake2b-64)` |
| `first_ns` / `last_ns` | `BIGINT` | no | — | Min run start / max run end-or-last-span |
| `run_count` | `INT` | no | 0 | |
| `input_tokens` / `output_tokens` / `cached_input_tokens` | `BIGINT` | no | 0 | Sum over runs |
| `cost_nusd` | `BIGINT` | no | 0 | Sum of priced run costs |
| `includes_unpriced` | `BOOL` | no | false | Any run unpriced (REQ-036.b) |
| `updated_at_ns` | `BIGINT` | no | now | |

**Indexes**

| Name | Columns | Justification |
|---|---|---|
| `uq_sessions_id` | `id` unique | `get_session` (API §6.1) |
| `uq_sessions_ext` | `project_pk, external_id` unique | Upsert by natural key (write path) |
| `ix_sessions_project_last` | `project_pk, last_ns DESC, pk` | Session list default sort plus keyset pagination plus time range (Q9) |

A Session with `run_count = 0` after a purge is deleted by the purge job.

### 2.9 `runs`

**Purpose:** One row per (Project, trace) [REQ-032, 100, 101, 106]. **Volume:** ~1/20 of spans
(50k runs per 1M spans). Read on every list and dashboard query.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | Run surrogate. Spans reference it |
| `project_pk` | `BIGINT` FK CASCADE | no | — | |
| `trace_id` | `BYTES(16)` | no | — | |
| `source_pk` | `BIGINT` FK → `sources.pk` | yes | — | Root span's Source, else the earliest span's |
| `source_count` | `SMALLINT` | no | 1 | Distinct Sources in the trace (multi-service traces) |
| `session_pk` | `BIGINT` FK → `sessions.pk` ON DELETE SET NULL | yes | — | Per REQ-036.c |
| `status` | `TEXT(12)` | no | `in_progress` | `CHECK IN ('in_progress','ok','error','incomplete')` (REQ-032.b) |
| `start_ns` | `BIGINT` | no | — | Min span start |
| `end_ns` | `BIGINT` | yes | — | Root end if root present, else NULL |
| `duration_ns` | `BIGINT` | yes | — | `end_ns − start_ns` if root present |
| `last_span_ns` | `BIGINT` | no | — | Max span end. Drives finalization and age purge |
| `root_span_pk` | `BIGINT` | yes | — | Root span (no FK, to avoid cycles; same trace) |
| `root_name` | `TEXT(256)` | yes | — | |
| `agent_name` | `TEXT(256)` | yes | — | Root span's mapped agent name, else the earliest agent-category span's |
| `is_genai` | `BOOL` | no | false | REQ-032.d |
| `models` | `JSON` | no | `[]` | Distinct effective models of LLMCalls, sorted (display only; filtering uses `llm_calls`) |
| `input_tokens` / `output_tokens` / `cached_input_tokens` / `cache_write_tokens` / `reasoning_tokens` | `BIGINT` | no | 0 | Sum over LLMCalls (DD-012) |
| `usage_source` | `TEXT(8)` | no | `none` | `CHECK IN ('llm','agent','none')` (DD-012 fallback) |
| `cost_nusd` | `BIGINT` | yes | — | Sum of priced LLMCall costs. NULL if the Run has LLMCalls and all are unpriced |
| `unpriced_calls` | `INT` | no | 0 | `unpriced` flag in DTO = `unpriced_calls > 0` |
| `llm_calls` / `tool_calls` / `error_spans` / `span_count` | `INT` | no | 0 | |
| `missing_parents` | `INT` | no | 0 | Spans whose parent is not stored (REQ-037.b placeholder) |
| `bytes_est` | `BIGINT` | no | 0 | Sum of `spans.size_bytes`. Size-based retention (§6) |
| `updated_at_ns` | `BIGINT` | no | now | Last recompute |

**Indexes** (each tied to a critical query in §4)

| Name | Columns | Type | Justification |
|---|---|---|---|
| `uq_runs_trace` | `project_pk, trace_id` | unique | Run upsert (write path). `resolve_trace` (Q4) |
| `ix_runs_project_start` | `project_pk, start_ns DESC, pk DESC` | composite | Run list default sort, time range, keyset (Q1). KPIs and runs-over-time (Q10) |
| `ix_runs_project_status_start` | `project_pk, status, start_ns DESC` | composite | Status filter (Q1 variant) |
| `ix_runs_project_source_start` | `project_pk, source_pk, start_ns DESC` | composite | Source filter. Cost by Source (Q10) |
| `ix_runs_session_start` | `session_pk, start_ns` | composite | Session detail (Q9b). Session recompute |
| `ix_runs_project_last` | `project_pk, last_span_ns` | composite | Age purge (Q13) |
| `ix_runs_inprogress` | `last_span_ns` WHERE `status = 'in_progress'` | partial | Finalizer (Q12) |

Sorting by duration, tokens or cost is applied to the time-range-filtered set without a dedicated
index. That is ≤ 50k rows at 1M spans and is verified by S2 (REQ-303).

### 2.10 `spans`

**Purpose:** Every accepted OTel span, post-redaction [REQ-030, 037, 038, 103, 151.e].
**Volume:** the dominant table. 1M rows ≈ 1–2 GB with content (estimate, measured in S2).

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | Also the FTS rowid and the derived-row key |
| `project_pk` | `BIGINT` FK CASCADE | no | — | Denormalized for scoping and FK cascade |
| `run_pk` | `BIGINT` FK → `runs.pk` ON DELETE CASCADE | no | — | Run stub upserted first in the transaction (API §5.1) |
| `span_id` | `BYTES(8)` | no | — | |
| `parent_span_id` | `BYTES(8)` | yes | — | NULL = root |
| `source_pk` | `BIGINT` FK → `sources.pk` | no | — | |
| `resource_pk` | `BIGINT` FK → `resources.pk` | no | — | |
| `name` | `TEXT(1024)` | no | — | Post-redaction. Truncated at 1,024 chars |
| `otel_kind` | `SMALLINT` | no | 0 | OTLP `SpanKind` 0–5 |
| `category` | `TEXT(14)` | no | `generic` | `CHECK IN ('llm','tool','agent','approval','generic_genai','generic')` (TD §5.3 classification) |
| `is_genai` | `BOOL` | no | false | Any `gen_ai.*` or mapped key present |
| `start_ns` / `end_ns` | `BIGINT` | no | — | `CHECK (end_ns >= start_ns)` |
| `status_code` | `SMALLINT` | no | 0 | 0 unset, 1 ok, 2 error |
| `status_message` | `TEXT` | yes | — | Post-redaction |
| `scope_name` / `scope_version` | `TEXT(256)` / `TEXT(64)` | yes | — | Instrumentation scope |
| `attributes` | `JSON` | no | `{}` | Span attributes (§1.4), raw minus redacted/dropped |
| `events` | `JSON` | no | `[]` | `[{"name","time_ns","attributes"}]` |
| `links` | `JSON` | no | `[]` | `[{"trace_id_hex","span_id_hex","attributes"}]` |
| `dropped_counts` | `JSON` | no | `{}` | OTLP `dropped_*_count` values if non-zero |
| `dropped_keys` | `JSON` | no | `[]` | Keys removed by the content toggle (REQ-040.a) |
| `agent_name` | `TEXT(256)` | yes | — | Mapped (for the Run recompute) |
| `session_ext` | `TEXT(512)` | yes | — | Mapped session external ID (REQ-036.c) |
| `agent_usage_input` / `agent_usage_output` | `BIGINT` | yes | — | Agent-level reported usage, only on agent spans (DD-012 fallback) |
| `flags` | `SMALLINT` | no | 0 | Bitmask: 1 truncated, 2 redacted, 4 content_dropped, 8 redaction_timeout |
| `mapping_version` | `SMALLINT` | no | — | `MAPPING_VERSION` used (REQ-030.c) |
| `size_bytes` | `INT` | no | — | Serialized size of the stored row's JSON + text (retention estimate) |
| `received_at_ns` | `BIGINT` | no | — | Commit group time |

**Indexes**

| Name | Columns | Type | Justification |
|---|---|---|---|
| `uq_spans_run_span` | `run_pk, span_id` | unique | **Idempotency** (insert-or-ignore, REQ-038). Trace detail fetch of all spans of a run (Q5). Run recompute scans by `run_pk` |
| `ix_spans_root_attrs` (PG only) | GIN `attributes jsonb_ops` WHERE `parent_span_id IS NULL` | partial GIN | Root-span attribute filters `eq`/`exists` (Q3) |

No index on `start_ns` and none on `name`: spans are always reached through `run_pk`, FTS or the
derived tables. SQLite root-attribute filters use `json_extract` on root spans joined from the
time-range-filtered runs (Q3), bounded by the run index.

### 2.11 `llm_calls`

**Purpose:** Inference operations [REQ-033.a, 061, 104, 106.b/c]. **Volume:** ~15–30 % of spans.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `span_pk` | `BIGINT` PK, FK → `spans.pk` ON DELETE CASCADE | no | — | |
| `run_pk` | `BIGINT` FK CASCADE | no | — | |
| `project_pk` | `BIGINT` | no | — | Denormalized for aggregates |
| `start_ns` / `duration_ns` | `BIGINT` | no | — | |
| `operation` | `TEXT(32)` | no | — | `chat`, `text_completion`, `generate_content`, `embeddings` |
| `provider` | `TEXT(64)` | yes | — | |
| `request_model` / `response_model` | `TEXT(200)` | yes | — | |
| `model` | `TEXT(200)` | no | `'unknown'` | Effective = response ?? request ?? `'unknown'` (grouping and filter key) |
| `input_tokens` / `output_tokens` | `BIGINT` | yes | — | |
| `cached_input_tokens` / `cache_write_tokens` / `reasoning_tokens` | `BIGINT` | yes | — | |
| `cost_nusd` | `BIGINT` | yes | — | NULL ⇔ unpriced |
| `unpriced` | `BOOL` | no | — | |
| `pricing_version` | `INT` FK → `pricing_versions.version` | yes | — | Version used (REQ-061.a) |
| `status` | `TEXT(5)` | no | `ok` | `CHECK IN ('ok','error')` |
| `error_type` | `TEXT(128)` | yes | — | |
| `finish_reasons` | `JSON` | no | `[]` | |
| `params` | `JSON` | no | `{}` | Normalized request parameters (TD §5.3) |
| `content_dropped` | `BOOL` | no | false | REQ-040.a |

**Messages are not duplicated here.** The normalized `Message` list (REQ-034, API `LLMCallView`)
is computed at read time from the span's stored attributes and events by the pure
`core.semconv.messages` normalizer. The ingest search document uses the same function. That
keeps one copy of the largest payload, and means a mapping fix improves old data without a
rebuild (see §9, note N1).

**Indexes**

| Name | Columns | Justification |
|---|---|---|
| `ix_llm_project_start` | `project_pk, start_ns, model` (PG: `INCLUDE (input_tokens, output_tokens, cached_input_tokens, cost_nusd, duration_ns, operation)`; SQLite: those columns appended to the key for covering) | Tokens/cost by model and latency by operation over a time range (Q10b/c) |
| `ix_llm_project_model_run` | `project_pk, model, run_pk` | Run filter "used model X" via `EXISTS` (Q2) |
| `ix_llm_run` | `run_pk` | Run recompute. Trace page LLM table (Q5b) |

### 2.12 `tool_calls`

**Purpose:** Tool executions [REQ-033.b, 106.d]. **Volume:** ~10–20 % of spans.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `span_pk` | `BIGINT` PK, FK CASCADE | no | — | |
| `run_pk` | `BIGINT` FK CASCADE | no | — | |
| `project_pk` | `BIGINT` | no | — | |
| `start_ns` / `duration_ns` | `BIGINT` | no | — | |
| `tool_name` | `TEXT(256)` | no | `'unknown'` | |
| `tool_call_id` | `TEXT(256)` | yes | — | |
| `tool_type` | `TEXT(32)` | yes | — | `function`, `extension`, `datastore` |
| `status` | `TEXT(5)` | no | — | `CHECK IN ('ok','error')` |
| `error_type` | `TEXT(128)` | yes | — | |
| `content_dropped` | `BOOL` | no | false | |

Arguments and results are read at query time from the span attributes via the mapping (same
rationale as messages).

**Indexes:** `ix_tool_project_start (project_pk, start_ns, tool_name, status, duration_ns)` for
tool stats and top tools (Q10d); `ix_tool_project_name_run (project_pk, tool_name, run_pk)` for
the tool filter (Q2); `ix_tool_run (run_pk)` for recompute and the trace page.

### 2.13 `approvals`

**Purpose:** Human approvals, deferrals, elicitations [REQ-033.c v1.1, 106.f]. **Volume:** small.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `span_pk` | `BIGINT` PK, FK CASCADE | no | — | |
| `run_pk` | `BIGINT` FK CASCADE | no | — | |
| `project_pk` | `BIGINT` | no | — | |
| `approval_key` | `TEXT(256)` | yes | — | `reflex_*.approval.id` |
| `kind` | `TEXT(12)` | no | — | `CHECK IN ('approval','deferral','elicitation')` |
| `decision` | `TEXT(10)` | no | — | `CHECK IN ('approved','rejected','edited','timeout','pending')` |
| `approver` | `TEXT(256)` | yes | — | Post-redaction |
| `requested_ns` | `BIGINT` | no | — | Span start |
| `decided_ns` | `BIGINT` | yes | — | Span end unless `pending` |
| `wait_ns` | `BIGINT` | yes | — | `decided_ns − requested_ns` |
| `tool_call_id` / `tool_name` / `node_name` | `TEXT(256)` | yes | — | Related tool or node |

**Indexes:** `ix_appr_project_requested (project_pk, requested_ns, decision, wait_ns)` (approval
outcomes and median wait, Q10f); `ix_appr_run (run_pk)`.

### 2.14 `graph_nodes`

**Purpose:** Agent-graph node executions [REQ-033.d, 106.c, 153]. **Volume:** ~5–15 % of spans.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `span_pk` | `BIGINT` PK, FK CASCADE | no | — | |
| `run_pk` | `BIGINT` FK CASCADE | no | — | |
| `project_pk` | `BIGINT` | no | — | |
| `node_key` | `TEXT(256)` | no | — | `graph.node.id` if present, else node name |
| `node_name` | `TEXT(256)` | no | — | Display label |
| `node_kind` | `TEXT(8)` | no | — | `CHECK IN ('agent','workflow','node')` |
| `explicit_parent_key` | `TEXT(256)` | yes | — | `graph.node.parent_id` / handoff source when the dialect provides it |
| `step` | `INT` | yes | — | `langgraph_step` |
| `agent_name` | `TEXT(256)` | yes | — | |
| `start_ns` / `end_ns` | `BIGINT` | no | — | |
| `status` | `TEXT(5)` | no | — | `CHECK IN ('ok','error')` |

Edges and execution counts are assembled at read time by `query.graph` (TD DD-011) from these
rows plus the span parent chain.
**Indexes:** `ix_gn_project_start (project_pk, start_ns, node_name, end_ns)` for latency by node
(Q10c); `ix_gn_run (run_pk)` for the graph view.

### 2.15 `span_search`

**Purpose:** Full-text search documents [REQ-073, 101.g; TD DD-006].

- **SQLite:** `CREATE VIRTUAL TABLE span_search USING fts5(doc, content='', contentless_delete=1,
  tokenize='unicode61 remove_diacritics 2')`. `rowid` = `spans.pk`. Requires SQLite ≥ 3.43 for
  `contentless_delete` (§9, note N2). A trigger
  `AFTER DELETE ON spans … DELETE FROM span_search WHERE rowid = old.pk` keeps it consistent
  (FTS tables cannot carry FKs).
- **PostgreSQL:** table `span_search(span_pk BIGINT PK FK → spans.pk ON DELETE CASCADE, run_pk
  BIGINT NOT NULL, project_pk BIGINT NOT NULL, tsv TSVECTOR NOT NULL)`.
  `tsv = to_tsvector('simple', doc)`, computed in the INSERT; the document text is not stored.
- **Indexes (PG):** `ix_search_tsv` GIN (`tsv`) for term search (Q3); `ix_search_run (run_pk)`
  for the run join.

Document = span name + message text parts + tool arguments/results + error/exception messages,
post-redaction, ≤ 32 KiB, built in the ingest normalizer and stored **after**
`search_normalize` (TD DD-006: NFKD, no diacritics, lowercase, non-alphanumerics → space). Non-GenAI spans index name and status
message only.

### 2.16 `metric_points`

**Purpose:** GenAI metric data points [REQ-005, 107]. **Volume:** ~1 point per series per export
interval (typically 60 s).

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `project_pk` | `BIGINT` FK CASCADE | no | — | |
| `source_pk` | `BIGINT` FK → `sources.pk` | no | — | |
| `name` | `TEXT(128)` | no | — | Only `gen_ai.*` (REQ-005.b) |
| `kind` | `TEXT(9)` | no | — | `CHECK IN ('sum','gauge','histogram')` |
| `unit` | `TEXT(32)` | yes | — | |
| `is_monotonic` / `temporality` | `BOOL` / `SMALLINT` | yes | — | Sums: monotonic flag, 1 delta / 2 cumulative |
| `start_ns` / `time_ns` | `BIGINT` | no | — | |
| `attrs_hash` | `BYTES(16)` | no | — | BLAKE2b-128 of canonical point attributes |
| `attributes` | `JSON` | no | `{}` | |
| `model` | `TEXT(200)` | yes | — | Extracted `gen_ai.response.model` ?? `gen_ai.request.model` |
| `token_type` | `TEXT(16)` | yes | — | `input`/`output`/`cached_input`/`cache_write`/`reasoning`, from `gen_ai.token.type` (legacy) or the metric name (new counters) |
| `value_int` / `value_double` | `BIGINT` / `DOUBLE` | yes | — | Sum and gauge (a float here is acceptable: telemetry values, not money) |
| `count` / `sum` | `BIGINT` / `DOUBLE` | yes | — | Histograms |
| `bucket_counts` / `explicit_bounds` | `JSON` | yes | — | Histograms |

**Indexes:** `uq_metric_point (project_pk, source_pk, name, attrs_hash, start_ns, time_ns)`
unique (idempotent re-exports); `ix_mp_project_name_time (project_pk, name, time_ns)` (Q11).

### 2.17 `saved_filters`

**Purpose:** Named Runs filters per Project [REQ-102]. **Volume:** tens per Project.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `id` | `TEXT(26)` | no | ULID | |
| `project_pk` | `BIGINT` FK CASCADE | no | — | |
| `name` | `TEXT(120)` | no | — | |
| `query` | `TEXT(4096)` | no | — | Canonical URL query string per API §6.2 (validated on save by parsing it into `RunFilter`) |
| `created_by` | `TEXT(128)` | yes | — | |
| `created_at_ns` / `updated_at_ns` | `BIGINT` | no | now | |

**Indexes:** `uq_saved_filters_id (id)`; `uq_saved_filters_name (project_pk, name)` unique →
`Conflict` (API §6.6).

### 2.18 `purge_log`

**Purpose:** Retention audit for Settings [REQ-092, 173.b]. **Volume:** ≤ 24/day. Trimmed to the
last 500 rows.

| Column | Type | Null | Default | Description |
|---|---|---|---|---|
| `pk` | `PK` | no | auto | |
| `trigger` | `TEXT(9)` | no | — | `CHECK IN ('scheduled','manual')` |
| `started_ns` / `finished_ns` | `BIGINT` | no / yes | — | |
| `deleted_traces_age` / `deleted_traces_size` | `INT` | no | 0 | |
| `deleted_bytes_est` | `BIGINT` | no | 0 | |
| `error_class` | `TEXT(128)` | yes | — | Failure, class name only (Art. 3) |

### 2.19 Conditional: `rollup_hourly` (only if spike S2 fails REQ-305)

> **Status: not adopted** (spike S2, P0-10.1; Delta Spec `changes/2026-10-req-305-percentile-budget/`).
> Kept as the reserved design for DD-005 ladder step 1, reopened only if the reference-machine
> benchmark misses REQ-305.b.

Reserved design, created by a later migration `0002_rollups` **only** if S2 adopts ladder step 1
(TD DD-005). It would be maintained by the writer for newly inserted rows and decremented by
purge:

`rollup_hourly(project_pk, source_pk, hour_ns, dim_kind ∈ {model, operation, tool, node},
dim_value, calls, errors, input_tokens, output_tokens, cached_input_tokens, cost_nusd,
unpriced_calls, lat_hist JSON /* 64 log2 buckets of duration */)`, PK
`(project_pk, source_pk, hour_ns, dim_kind, dim_value)`.

Percentiles from `lat_hist` are approximate (bucket upper bound). The PRD tolerates this only via
a delta spec, which would be raised if this path is taken.

---

## 3. Relationships

| From | To | Type | FK column | On delete | Note |
|---|---|---|---|---|---|
| `api_keys`, `redaction_rule_sets`, `saved_filters`, `sources`, `resources`, `sessions`, `runs`, `spans`, `metric_points` | `projects` | N:1 | `project_pk` | CASCADE | REQ-170.d deletes everything |
| `runs` | `sessions` | N:1 | `session_pk` | SET NULL | The Session is recomputed or removed by purge |
| `runs` | `sources` | N:1 | `source_pk` | RESTRICT | Sources are only deleted with their Project |
| `spans` | `runs` | N:1 | `run_pk` | CASCADE | Purge deletes runs, which cascades to spans |
| `spans` | `resources`, `sources` | N:1 | `resource_pk`, `source_pk` | RESTRICT | Orphan resources cleaned by purge |
| `llm_calls`, `tool_calls`, `approvals`, `graph_nodes` | `spans` | 1:0..1 | `span_pk` | CASCADE | Also carry `run_pk` (CASCADE) for direct run access |
| `span_search` (PG) | `spans` | 1:0..1 | `span_pk` | CASCADE | SQLite: delete trigger |
| `llm_calls` | `pricing_versions` | N:1 | `pricing_version` | RESTRICT | Versions are never deleted |
| `pricing_entries` | `pricing_versions` | N:1 | `version` | RESTRICT | |

Foreign keys are enforced on both backends (SQLite `PRAGMA foreign_keys=ON` on every connection,
asserted by a startup check).

---

## 4. Critical queries

The SQL is PostgreSQL-flavored. SQLite equivalents are produced by `store.dialect`
(`SqlDialectOps`), with the differences noted. Targets refer to the PRD §7.1 datasets (100k spans
≈ 5k runs; 1M spans ≈ 50k runs). Every query is scoped by `project_pk IN (:allowed)`, which is
never omitted (API §5).

### Q1: Run list, default sort, keyset [REQ-100, 101.a–c, 303]

```sql
SELECT r.* FROM runs r
WHERE r.project_pk = ANY(:projects)
  AND r.start_ns >= :from AND r.start_ns < :to
  AND (:statuses IS NULL OR r.status = ANY(:statuses))
  AND (:cursor_start IS NULL OR (r.start_ns, r.pk) < (:cursor_start, :cursor_pk))
ORDER BY r.start_ns DESC, r.pk DESC
LIMIT :limit + 1;           -- +1 to compute has_next
-- Index: ix_runs_project_start (or ix_runs_project_status_start) | Target p95 < 300 ms (REQ-303)
```

SQLite: row-value comparison is supported (≥ 3.15). `ANY(:x)` becomes `IN (…)` with expanded
bind parameters.

### Q2: Run filters on derived tables [REQ-101.d/e/h]

```sql
-- model / tool filters (AND across kinds, OR within a kind)
AND EXISTS (SELECT 1 FROM llm_calls l WHERE l.project_pk = r.project_pk
            AND l.model = ANY(:models) AND l.run_pk = r.pk)
AND EXISTS (SELECT 1 FROM tool_calls t WHERE t.project_pk = r.project_pk
            AND t.tool_name = ANY(:tools) AND t.run_pk = r.pk)
-- numeric ranges
AND r.duration_ns BETWEEN :dmin AND :dmax
AND (r.input_tokens + r.output_tokens) BETWEEN :tmin AND :tmax
AND (r.cost_nusd BETWEEN :cmin AND :cmax OR (:include_unpriced AND r.unpriced_calls > 0))
AND r.session_pk = :session_pk
-- Index: ix_llm_project_model_run, ix_tool_project_name_run (probe by run_pk)
```

### Q3: Attribute and text filters [REQ-101.f/g, 073]

```sql
-- root-span attribute equality / existence (key bound as a parameter, never interpolated)
AND EXISTS (SELECT 1 FROM spans s WHERE s.pk = r.root_span_pk
            AND s.attributes @> jsonb_build_object(:key, :value::jsonb))     -- eq
AND EXISTS (SELECT 1 FROM spans s WHERE s.pk = r.root_span_pk AND s.attributes ? :key)  -- exists
-- resource attribute: join spans s (root) → resources res, same operators on res.attributes
-- full text
AND EXISTS (SELECT 1 FROM span_search q WHERE q.run_pk = r.pk
            AND q.tsv @@ to_tsquery('simple', :tsquery))
```

SQLite:
- Equality uses `json_extract(s.attributes, '$."' || :key_escaped || '"') = :value`. The key is
  validated by the TD §6.1 regex and passed as a bound path parameter, so it is never interpolated.
- Existence uses `json_type(...) IS NOT NULL`.
- Text uses `r.pk IN (SELECT s.run_pk FROM spans s JOIN span_search f ON f.rowid = s.pk WHERE
  span_search MATCH :fts AND s.project_pk = …)`.

Index: PG `ix_spans_root_attrs` (GIN) and `ix_search_tsv`; SQLite uses the FTS5 index plus the
`uq_spans_run_span` probe. Target: within the Q1 budget (REQ-303 "any single filter or text
search").

### Q4: Resolve trace ID [REQ-103.b]

```sql
SELECT r.pk, p.id, p.slug FROM runs r JOIN projects p ON p.pk = r.project_pk
WHERE r.trace_id = :trace_id_bytes AND r.project_pk = ANY(:projects);
-- Index: uq_runs_trace (range on project_pk prefix + equality) | Target < 10 ms
```

### Q5: Trace detail [REQ-103.a, 150, 304]

```sql
SELECT s.pk, s.span_id, s.parent_span_id, s.name, s.category, s.start_ns, s.end_ns, s.status_code,
       s.status_message, s.flags, s.source_pk,
       l.*, t.*, a.*, g.*                                           -- LEFT JOINs on span_pk
FROM spans s
LEFT JOIN llm_calls l ON l.span_pk = s.pk  LEFT JOIN tool_calls t ON t.span_pk = s.pk
LEFT JOIN approvals a ON a.span_pk = s.pk  LEFT JOIN graph_nodes g ON g.span_pk = s.pk
WHERE s.run_pk = :run_pk ORDER BY s.start_ns, s.pk;
-- Q5b raw: SELECT attributes, events, links, resource FROM spans … WHERE pk = :span_pk (lazy, drawer/raw tab)
-- Index: uq_spans_run_span (run_pk prefix) | Target p95 < 500 ms at 1,000 spans (REQ-304)
```

The tree assembly (DFS order, depth, missing-parent placeholders, subtree token/cost sums) happens
in `query`. It is a single pass over the rows of one trace, which is presentation and not an
aggregate across rows (Art. 6 applies to dashboard aggregates).

### Q6: Write path, Run stub and span insert [REQ-038, API §5.1]

```sql
-- Rows sorted by (project_pk, trace_id): creates missing stubs AND row-locks existing Runs (DD-004
-- concurrency rule, Analyze A-01). DO NOTHING would not lock an existing row.
INSERT INTO runs (project_pk, trace_id, start_ns, last_span_ns, status, updated_at_ns)
VALUES … ON CONFLICT (project_pk, trace_id) DO UPDATE SET updated_at_ns = excluded.updated_at_ns
RETURNING pk, trace_id;
INSERT INTO spans (…) VALUES … ON CONFLICT (run_pk, span_id) DO NOTHING RETURNING pk, run_pk, span_id;
-- per-span derived rows inserted only for RETURNed spans; duplicates = sent − returned
```

SQLite ≥ 3.35 supports `RETURNING`. Multi-row VALUES are chunked to stay under 32,766 bind
parameters.

### Q7: Run recompute, from scratch per affected trace [REQ-032, 037, DD-004]

```sql
WITH affected(run_pk) AS (SELECT unnest(:run_pks)),                 -- SQLite: temp table
s AS (SELECT sp.* FROM spans sp JOIN affected a ON a.run_pk = sp.run_pk),
root AS (SELECT DISTINCT ON (run_pk) * FROM s WHERE parent_span_id IS NULL
         ORDER BY run_pk, start_ns, pk),                            -- SQLite: ROW_NUMBER() window
agg AS (SELECT run_pk, min(start_ns) start_ns, max(end_ns) last_span_ns, count(*) span_count,
               sum(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) error_spans,
               bool_or(is_genai) is_genai, sum(size_bytes) bytes_est,
               count(DISTINCT source_pk) source_count
        FROM s GROUP BY run_pk),
orphans AS (SELECT c.run_pk, count(*) n FROM s c
            WHERE c.parent_span_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM s p WHERE p.run_pk = c.run_pk AND p.span_id = c.parent_span_id)
            GROUP BY c.run_pk),
llm AS (SELECT run_pk, count(*) n, sum(coalesce(input_tokens,0)) it, …, sum(cost_nusd) cost,
               sum(CASE WHEN unpriced THEN 1 ELSE 0 END) unpriced_calls,
               jsonb_agg(DISTINCT model ORDER BY model) models            -- SQLite: json_group_array over a DISTINCT subquery
        FROM llm_calls WHERE run_pk IN (SELECT run_pk FROM affected) GROUP BY run_pk),
tools AS (…count per run_pk…)
UPDATE runs r SET
  start_ns = agg.start_ns, last_span_ns = agg.last_span_ns, span_count = agg.span_count, …,
  root_span_pk = root.pk, root_name = root.name, end_ns = root.end_ns,
  duration_ns = root.end_ns - agg.start_ns,
  status = CASE WHEN root.pk IS NOT NULL THEN (CASE WHEN root.status_code = 2 THEN 'error' ELSE 'ok' END)
                WHEN agg.last_span_ns > :now_ns - :window_ns THEN 'in_progress' ELSE 'incomplete' END,
  agent_name = coalesce(root.agent_name, (SELECT agent_name FROM s WHERE s.run_pk = r.pk AND category = 'agent'
                                          AND agent_name IS NOT NULL ORDER BY start_ns, pk LIMIT 1)),
  usage_source = CASE WHEN llm.n > 0 THEN 'llm' WHEN <agent usage present> THEN 'agent' ELSE 'none' END,
  input_tokens = CASE WHEN llm.n > 0 THEN llm.it ELSE coalesce(<earliest agent span usage>, 0) END, …,
  cost_nusd = llm.cost, unpriced_calls = coalesce(llm.unpriced_calls, 0),
  source_pk = coalesce(root.source_pk, <earliest span source>),
  session_pk = (SELECT pk FROM sessions WHERE project_pk = r.project_pk
                AND external_id = coalesce(root.session_ext, <earliest span session_ext>)),
  updated_at_ns = :now_ns
FROM agg LEFT JOIN root USING (run_pk) LEFT JOIN llm USING (run_pk) …
WHERE r.pk = agg.run_pk;
-- Then sessions: lock the affected session_pks (old and new) with
--   SELECT pk FROM sessions WHERE pk IN (…) ORDER BY pk FOR UPDATE   (PG; DD-004 concurrency rule)
-- and recompute run_count/first/last/tokens/cost/includes_unpriced from runs.
-- Index: uq_spans_run_span, ix_llm_run, ix_tool_run, ix_runs_session_start
```

The exact statements are dialect-specific and unit-tested against a Python reference
implementation of the same rules (property tests, REQ-365). Clock: `:now_ns` is injected so tests
are deterministic.

### Q8: KPIs [REQ-106.g]

```sql
SELECT count(*) runs,
       avg((status = 'error')::int) error_rate,
       sum(input_tokens + output_tokens) tokens, sum(cost_nusd) cost_nusd,
       sum((unpriced_calls > 0)::int) unpriced_runs,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ns) p95_duration,  -- SQLite: nearest-rank window
       count(DISTINCT session_pk) active_sessions
FROM runs WHERE project_pk = ANY(:projects) AND start_ns >= :from AND start_ns < :to
  AND (:sources IS NULL OR source_pk = ANY(:sources));
-- Index: ix_runs_project_start / ix_runs_project_source_start
```

**Percentile definition (both backends):** nearest-rank, `percentile_disc` semantics
(value at rank ⌈p·N⌉, with the product computed in **IEEE double precision** exactly as PostgreSQL
does: `0.95 × 20 = 19.000000000000004 → rank 20`). Several percentiles of one value are computed in
one sort (`percentile_disc(ARRAY[…])` on PG, one window with conditional picks on SQLite; S2).
SQLite computes it as
`ROW_NUMBER() OVER (PARTITION BY g ORDER BY d)` compared with `ceil(p * count(*) OVER (PARTITION BY g))`,
and the `percentile_sql(p, value, group)` builder emits the right form per dialect. Contract tests
assert exact equality across backends.

### Q9: Sessions [REQ-105]

```sql
-- Q9a list
SELECT * FROM sessions WHERE project_pk = ANY(:p) AND last_ns >= :from AND first_ns < :to
  AND (last_ns, pk) < (:c_last, :c_pk) ORDER BY last_ns DESC, pk DESC LIMIT :n + 1;
-- Q9b detail
SELECT * FROM runs WHERE session_pk = :s ORDER BY start_ns;        -- cumulative sums via window in SQL
-- Index: ix_sessions_project_last, ix_runs_session_start
```

### Q10: Dashboard aggregates [REQ-106.a–f, 305]

```sql
-- a) runs over time by status (bucket = floor(start_ns / :bucket_ns) * :bucket_ns)
SELECT (start_ns / :b) * :b bucket, status, count(*) FROM runs WHERE <scope> GROUP BY 1, 2;
-- b) tokens & cost by model (+ over time)
SELECT model, sum(input_tokens), sum(output_tokens), sum(cached_input_tokens), sum(cost_nusd),
       sum(unpriced::int) FROM llm_calls WHERE <scope on project_pk, start_ns> GROUP BY model;
-- c) latency p50/p95 per operation and per node
SELECT 'op' dim, operation g, duration_ns d FROM llm_calls WHERE <scope>
UNION ALL SELECT 'op', 'execute_tool', duration_ns FROM tool_calls WHERE <scope>
UNION ALL SELECT 'op', 'invoke_agent', end_ns - start_ns FROM graph_nodes WHERE <scope> AND node_kind = 'agent'
UNION ALL SELECT 'node', node_name, end_ns - start_ns FROM graph_nodes WHERE <scope>
-- → percentile_sql(0.5 / 0.95 / 0.99, d, (dim, g))
-- d) tool stats + top N
SELECT tool_name, count(*), sum((status = 'error')::int) FROM tool_calls WHERE <scope>
  GROUP BY tool_name ORDER BY count(*) DESC LIMIT :n;
-- e) cost & tokens by Source
SELECT source_pk, sum(cost_nusd), sum(input_tokens + output_tokens), sum((unpriced_calls > 0)::int)
  FROM runs WHERE <scope> GROUP BY source_pk;
-- f) approvals
SELECT decision, count(*), percentile_disc(0.5) WITHIN GROUP (ORDER BY wait_ns)
  FROM approvals WHERE <scope> GROUP BY decision;
-- Indexes: ix_runs_project_start, ix_llm_project_start (covering), ix_tool_project_start,
--          ix_gn_project_start, ix_appr_project_requested
-- Target: whole set p95 < 1.5 s (SQLite) / < 1 s (PG) at 1M spans, 30 days (REQ-305; S2 gate)
```

Each aggregate returns ≤ (buckets × groups) rows, which the REQ-106.h test asserts (bounded rows
fetched).

### Q11: Metric token usage [REQ-107]

```sql
SELECT (time_ns / :b) * :b bucket, model, token_type,
       sum(coalesce(value_int, 0)) FILTER (WHERE kind = 'sum' AND temporality = 1)   -- delta counters
     + sum(coalesce(sum, 0))       FILTER (WHERE kind = 'histogram')                 -- legacy histogram
FROM metric_points WHERE project_pk = ANY(:p) AND name IN (:token_metric_names)
  AND time_ns >= :from AND time_ns < :to GROUP BY 1, 2, 3;
-- Cumulative counters are converted to deltas in SQL with LAG() over (series by attrs_hash ORDER BY time_ns).
-- Index: ix_mp_project_name_time
```

### Q12: Finalizer [REQ-032.b]

```sql
SELECT pk FROM runs WHERE status = 'in_progress' AND last_span_ns < :now_ns - :window_ns
ORDER BY project_pk, trace_id LIMIT 500 FOR UPDATE SKIP LOCKED;     -- PG; SQLite: plain SELECT
-- → Q7 recompute for those run_pks (flips to 'incomplete'); publish runs_finalized
-- SKIP LOCKED: a Run a writer holds is being recomputed by that writer anyway
-- Index: ix_runs_inprogress (partial)
```

### Q13: Retention purge [REQ-090, 091, 092]

```sql
-- age, per project with retention_max_age_s > 0
SELECT pk FROM runs WHERE project_pk = :p AND last_span_ns < :now_ns - :max_age_ns
ORDER BY last_span_ns LIMIT :batch;                        -- Index: ix_runs_project_last
SELECT pk FROM runs WHERE pk = ANY(:batch_pks)
ORDER BY project_pk, trace_id FOR UPDATE;                  -- PG: DD-004 lock order (A-01)
DELETE FROM runs WHERE pk = ANY(:batch_pks);               -- cascades to spans, derived rows, search
-- then lock + recompute the Sessions those runs belonged to (Q7 session step)
-- size: usage = (SELECT sum(bytes_est) FROM runs) + metric estimate;
-- while usage > limit: delete the globally oldest runs by last_span_ns (batch), until ≤ 90 % of limit
-- afterwards: delete sessions with no runs, orphan resources; SQLite: PRAGMA incremental_vacuum(N)
```

Each batch is its own transaction, run on the writer (SQLite) so that ingestion latency holds
(REQ-092.c).

### Q14: API key verification [REQ-006]

```sql
SELECT k.salt, k.secret_hash, k.revoked_at_ns, p.pk, p.id, p.settings_version
FROM api_keys k JOIN projects p ON p.pk = k.project_pk WHERE k.key_id = :key_id;
-- Index: uq_api_keys_key_id | Only on a cache miss (positive cache TTL ≤ 5 s)
```

---

## 5. Migrations

- **Tool:** Alembic, with the environment shipped in the package
  (`store/migrations/env.py`, async engine). Revisions are named `NNNN_<slug>.py`.
- **`0001_initial`** creates every table in §2 except §2.19. It branches on `op.get_bind().dialect.name` for:
  - FTS5 virtual table and delete trigger (SQLite) vs. `span_search` table and GIN indexes (PG)
  - partial indexes (both backends support them)
  - `INCLUDE` columns (PG only; SQLite gets extended key columns)
  - `BOOL`/`JSON` mappings

  It also inserts `app_meta.instance_id`/`created_at_ns`. **Pricing version 1 is not loaded by
  the migration** (Analyze A-11): the first-run bootstrap loads it when `pricing_versions` is
  empty, from `[pricing] file` or the bundled `data/pricing.csv` (REQ-060.b, API §8.2), through
  `save_pricing(…, author="bootstrap")`, in the same startup step that creates the `default`
  Project and its key (REQ-201). Until then ingest marks calls `unpriced`, which cannot happen in
  practice because bootstrap runs before the receivers start.
- **Policy:**
  - Forward-only within 0.x. Every migration has a `downgrade()` for development, but releases
    do not support downgrades: a newer schema means exit 4 (REQ-074.c).
  - Every migration is tested on SQLite and PG 15/18 in CI (contract job runs
    `upgrade head` from empty and from the previous release's schema fixture).
  - SQLite table rebuilds use `batch_alter_table`.
  - Large data migrations run in batches through the writer.
- **Concurrency:** PG `pg_advisory_lock(hashtext('agentlens:migrate'))` around upgrade
  (REQ-074.d). On SQLite the single process migrates before starting the ingest thread.
- **Derived-view changes** (mapping or pricing semantics) are **not** schema migrations. They bump
  `MAPPING_VERSION` and instruct `reflex-agentlens rebuild` (TD §9.2).

## 6. Retention and archiving

| Data | Rule | Mechanism | REQ |
|---|---|---|---|
| Traces (runs, spans, all derived rows, search docs) | Per-Project max age on `runs.last_span_ns` (default 30 d; `0` = forever) | Purge job Q13 (age) | 090 |
| Traces, global | Optional storage size limit on `sum(runs.bytes_est)` + metric estimate; delete oldest down to 90 % | Purge job Q13 (size) | 091 |
| `metric_points` | Same per-Project max age, on `time_ns` | Purge job | 090 |
| `sessions` with no runs, orphan `resources` | Deleted after each purge | Purge job | 090.b |
| `purge_log` | Keep the last 500 rows | Purge job | 173.b |
| Settings tables, `pricing_*`, `saved_filters`, `api_keys` | Never purged | — | 091.b |

**Why an estimate for size limits:** on PostgreSQL, deleted rows do not shrink relation size until
VACUUM, and on SQLite the file does not shrink without vacuuming. Both would make a physical-size
loop delete too much. `bytes_est` (the row payload size computed at ingest) is deterministic and
identical on both backends. Settings shows both the estimate and the physical size
(`page_count × page_size` for SQLite, `pg_total_relation_size` sum for PG) (REQ-173.a). No
archiving or cold tier in v0.1. Dataset export is v0.2.

## 7. Backup and recovery

Operator responsibility, documented in `docs/operations/backup.md`. agentlens adds no backup
machinery in v0.1.

| Aspect | SQLite (T1/T2) | PostgreSQL (T3) |
|---|---|---|
| Online backup | `sqlite3 agentlens.db ".backup /path/snap.db"` (safe with WAL), or Litestream (optional, external) | `pg_dump -Fc` / base backups + WAL archiving |
| Consistency | `.backup` is transactionally consistent | Standard PG |
| Secrets to back up | `{data_dir}/session_secret` (otherwise sessions and cursors are invalidated, which is harmless) | Same, plus the `.env` secrets |
| RPO / RTO | Operator-defined. Losing derived tables is recoverable via `rebuild` as long as `spans` survive | Operator-defined |
| Restore check | `reflex-agentlens db status` must report `CURRENT` | Same |

Backups contain post-redaction content only. Redaction happens before persistence (REQ-322), so
there is never unredacted data in a backup. Retention does not reach into backups; the docs state
this explicitly for compliance.

## 8. DTO ↔ table mapping (summary)

| API DTO (API §6) | Source tables | Conversion |
|---|---|---|
| `RunSummary` | `runs` ⋈ `sources` ⋈ `sessions(id)` ⋈ `projects(id)` | `trace_id` → hex. `cost_nusd` → `Decimal(cost) / 10**9` (`cost_usd`). `unpriced = unpriced_calls > 0` |
| `TraceDetail.spans[]` (`SpanNode`) | Q5 | Tree built in `query`. `label` from category plus mapped fields |
| `LLMCallView` | `llm_calls` + `spans.attributes/events` | `messages` via `core.semconv.messages` at read time |
| `ToolCallView` | `tool_calls` + `spans.attributes` | args/result via mapping at read time |
| `ApprovalView` | `approvals` | — |
| `AgentGraph` | `graph_nodes` + span parent chain | `query.graph` |
| `SessionSummary` / `SessionDetail` | `sessions`, `runs` | `cost_nusd` → `Decimal` |
| `DashboardData` | Q8, Q10, Q11 | Buckets → labels in `components` |
| `PricingTable` | `pricing_versions`, `pricing_entries` | `*_nusd_per_mtok` → `Decimal` USD per Mtok |
| `ApiKeyView` | `api_keys` | `al_<key_id>_…` prefix display only |

Field-level DTO definitions live in API §6.7; kind mapping in API §6.8.

**Money note (Constitution Art. 5):** the article requires `Decimal` in Python and fixed-precision
`NUMERIC` in SQL, never float. This model uses **fixed-point integers (`BIGINT` nano-USD)** on
both backends. SQLite has no exact `NUMERIC` storage (its NUMERIC affinity coerces to REAL), so
integers are the only fixed-precision representation that is exact and identical on both
backends. Python always handles these values as `Decimal`. Interpretation recorded in the
Constitution check below.

---

## 9. Notes on upstream artifacts

| ID | Artifact | Note |
|---|---|---|
| N1 | Tech Design §5.1 | Message and tool-I/O normalization lives in `core.semconv.messages` (pure, leaf), because `query` needs it at read time and must not import `ingest`. `ingest.normalize.messages` becomes a thin caller. Not a behavior change |
| N2 | Tech Design DD-008, Data Model metadata | Minimum SQLite raised from 3.38 to **3.43** (FTS5 `contentless_delete`, needed so purge can delete search rows). Python 3.11+ official builds and `python:3.13-slim` ship ≥ 3.45. The startup check enforces it |
| N3 | Constitution Art. 5 | Money as `BIGINT` nano-USD (see §8 note). Interpretation, not exception |
| N4 | API §5 `StorageBackend.storage_usage` | Returns both `estimated_bytes` (retention basis) and `physical_bytes` |

These are applied to the Tech Design (v1.1) and recorded in the Constitution amendments table
when this document is approved.

---

## Constitution check

| Article | How this data model complies |
|---|---|
| Art. 1 | `spans.attributes/events/links` keep every attribute raw (post-redaction). `mapping_version` per span. Derived rows are rebuildable |
| Art. 2 | Two backends, one logical schema, dialect differences confined to FTS, JSON operators, `INCLUDE` and percentiles. No extra infrastructure. Rollups (if ever) are plain tables |
| Art. 3 | Only post-redaction data reaches any table. Content-toggle drops are recorded by key name only. No plaintext keys (canary-scanned). Hard deletes on purge |
| Art. 4 | Keys as salt plus SHA-256. All filter values bound. Attribute keys validated and bound as JSON-path parameters |
| Art. 5 | Time in UTC ns `BIGINT`. Money fixed-point (nano-USD `BIGINT`, `Decimal` in Python; **interpretation N3**). Unpriced = NULL + flag. Pricing version per LLMCall. Idempotency via `uq_spans_run_span` + insert-or-ignore. Order independence via Q7 recompute-from-facts |
| Art. 6 | Every aggregate in §4 is SQL (Q8, Q10, Q11). Indexes justified by the query each serves |
| Art. 7 | N/A (no UI concerns in the schema) |
| Art. 8 | Contract tests on SQLite + PG 15/18 for every query and migration. Q7 checked against a Python reference model with property tests. EXPLAIN-plan assertions for Q1, Q2 and Q10 indexes |
| Art. 9 | `purge_log` for operator visibility |
| Art. 10 | Upstream notes N1–N4 listed for approval. Conditional §2.19 tied to spike S2 |

**Exceptions requested:** none. **Interpretation requiring confirmation:** N3 (money as fixed-point
integers).

---

## Change history

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0-draft.1 | 2026-10-30 | Ernesto Crespo (with Claude) | Initial draft |
| 1.0 | 2026-10-30 | Ernesto Crespo | Approved. Notes N1–N4 applied (Tech Design v1.1, Constitution amendments) |
| 1.0.1 | 2026-10-31 | Ernesto Crespo (with Claude) | From spike S2: percentile rank computed in double precision (parity), one-sort multi-percentile form; search document normalized before indexing (DD-006) |
| 1.1 | 2026-10-31 | Ernesto Crespo (with Claude) | Analyze A-01: Q6 Run stubs via `ON CONFLICT DO UPDATE … RETURNING` in sorted order (row locks), Session locks before recompute, finalizer `FOR UPDATE SKIP LOCKED`. A-11: pricing seed moved from `0001_initial` to first-run bootstrap. A-09: §2.19 not adopted |
| 1.2 | 2026-10-31 | Ernesto Crespo (with Claude) | Delta `changes/2026-10-p3-ingest-prep/`: `app_meta.session_key` and the post-redaction Session natural key (A-08) |
| 1.3 | 2026-10-31 | Ernesto Crespo (with Claude) | Delta `changes/2026-10-p4-query-prep/`: §8 points to API §6.7 for DTO fields (A-05) |
