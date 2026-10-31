# Analyze — reflex-agentlens v0.1 · 2026-10-31

> Read-only cross-artifact review (skill checklist `11-CHECKLIST-ANALYZE.md`), run before any
> implementation. Nothing was changed in the reviewed artifacts. The maintainer decides what to fix.
>
> Artifacts reviewed: Constitution v1.1 · PRD v1.1 · API Spec v1.1 · Tech Design v1.1 ·
> Data Model v1.0 · Implementation Plan v1.0 · Tasks v1.0 (P0 + P1 detailed, rolling wave).

Severities: **CRITICAL** = built as specified, the system would not meet the spec or would violate the
constitution · **HIGH** = a gap an implementing agent would fill by inventing · **MEDIUM** = friction
or drift · **LOW** = style or wording.

"Blocks" names the earliest task that must not start before the finding is resolved.

## Summary

| Category | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| 1 Coverage | — | 2 | 2 | 1 |
| 2 Constitution | — | 1 | 1 | — |
| 3 Ambiguity | — | — | 1 | 2 |
| 4 Terminology | — | 1 | 1 | 2 |
| 5 Unhappy paths | 1 | 1 | 3 | 1 |
| 6 Data | — | — | 2 | — |
| 7 Task executability | — | — | 1 | 2 |
| **Total (25)** | **1** | **5** | **11** | **8** |

What checked out (no finding): every one of the 103 PRD requirements maps to at least one plan
task (scripted check); Run status values are identical across PRD, API, Tech Design and Data Model;
the ingest check order is identical in API §3.3 and Tech Design Flow 1; exit codes, config keys and
env-var naming agree between API §8 and Tech Design DD-019; every Data Model index names the query it
serves and every hot query (Q1–Q14) has an index; money is fixed-point everywhere (N3); the
Constitution check is present in all seven artifacts with no unjustified exception.

## Findings

| # | Severity | Category | Finding | Artifacts | Suggestion | Blocks |
|---|---|---|---|---|---|---|
| A-01 | CRITICAL | Unhappy paths | **Concurrent recompute race on PostgreSQL.** With several writers (`collect` ×N, plus the finalizer and purge in `serve`), two transactions can insert spans of the *same* trace at the same time. Q6 creates the Run stub with `ON CONFLICT DO NOTHING`, which does not lock an existing row, and Q7 recomputes the Run from the spans each transaction can see. Under READ COMMITTED each writer sees only its own uncommitted spans plus committed ones, so the last commit overwrites the Run with a partial view (counts, tokens, status). The same applies to Session aggregates. This breaks REQ-072.b ("derived views consistent across processes"), REQ-205.c and, across processes, REQ-037.a. DD-004 argues order independence only for a single writer | Data Model Q6/Q7/Q12, TD DD-003/DD-004, PRD REQ-072.b | Amend DD-004 and Q6/Q7: take row locks on the affected Runs in a deterministic order before inserting spans (e.g. `INSERT … ON CONFLICT (project_pk, trace_id) DO UPDATE SET updated_at_ns = excluded.updated_at_ns RETURNING pk`, rows sorted by `trace_id`), then lock the affected Sessions (`SELECT … FOR UPDATE ORDER BY pk`) before recomputing them. Apply the same rule to finalizer, rebuild and cost recompute. Make the P2-06 multi-writer contract test interleave the *same* traces across writers | P2-01 |
| A-02 | HIGH | Constitution | **Specs are not under version control.** `specs/` is untracked on `develop`. Art. 10 and the spec-anchored rigor level require approved specs to live in the repo before implementation; the Plan/Tasks "Status" columns are meant to be resumable from git | Constitution Art. 10, repo state | Commit `specs/` (incl. this report) to `develop` via PR before the first task | P0-01.1 |
| A-03 | HIGH | Terminology | **Percentile function drift.** TD DD-005 and Tasks P0-09.2 say `percentile_cont` on PostgreSQL. Data Model Q8 and its "Percentile definition" say nearest-rank `percentile_disc` semantics with exact cross-backend equality. `percentile_cont` interpolates, so it would fail the exact-equality contract test and give different p50/p95 than SQLite | TD DD-005, Tasks P0-09.2, Data Model Q8/Q10 | Change DD-005 and P0-09.2 to `percentile_disc` (nearest-rank). Data Model is correct | P0-09.2 |
| A-04 | HIGH | Unhappy paths | **One bad batch fails the whole commit group.** The writer commits up to 5,000 spans from many requests and Projects in one transaction. Any statement error caused by a single batch (FK violation when a Project is deleted while its key is still in the ≤ 5 s auth cache, a CHECK violation from an unforeseen value, a too-long text) aborts the group, and every request in it gets an error. The TD failure table only covers "DB unreachable" | TD DD-003 + §3.3 error table, Data Model §2 CHECKs, PRD REQ-009 | Specify per-batch isolation: one SAVEPOINT per batch inside the group; on a batch error, roll back to the savepoint, resolve that batch's future with a non-retryable error (and its HTTP/gRPC status), and commit the rest. Add a test that deletes a Project mid-ingest | P3-06 |
| A-05 | HIGH | Coverage | **DTOs undefined at field level.** API §6.1 says "field-level types in the Data Model", but Data Model §8 only maps DTOs to tables. Undefined: `TokenCostTotals`, `ToolCallView`, `ApprovalView`, `SpanRaw`, `SessionSummary`, `SessionDetail`, `DashboardData`, `Kpis`, `TimeSeries`, `ChangeNotice`, `StorageStatus`, `ApiKeyView`, `PricingTable`, `Project`. Also `RunSummary` has no `usage_source`, although DD-012 requires a UI footnote when usage comes from agent spans | API §5/§6, Data Model §8, TD DD-012 | API v1.2: add a DTO appendix with every field and type; add `usage_source` to `RunSummary` | P2-07 (protocol return types), P4-01 |
| A-06 | HIGH | Coverage | **Storage protocol and query table gaps.** There is no protocol method for the raw span fetch (Data Model Q5b; needed by REQ-151.e), and `get_span_raw` is mentioned in a code comment but missing from the `QueryService` table (API §6.1). Protocol methods `rebuild`, `recompute_costs`, `finalize_stale_runs`, `distinct_values`, `list_unpriced_models`, `touch_api_key` and `write_metric_points` are not named in any plan task, so ownership and contract-test coverage are unclear | API §5, §6.1, Plan P2/P3/P5 | API v1.2: add `get_span_raw` to `StorageBackend` and `QueryService`. When detailing P2, list every protocol method under exactly one task and in the contract suite | P2 detailing |
| A-07 | MEDIUM | Constitution | **Truncation runs before redaction.** TD Flow 1 step 9 is "validate → truncate → redaction". A secret that straddles the 256 KiB limit is cut, may no longer match its pattern, and its prefix is stored unredacted (Art. 3, REQ-039.a, REQ-322) | TD §3.3 Flow 1, Plan P3-02/P3-04 | Redact first, then truncate (the per-rule regex budget already bounds the cost). Add a canary case with a secret across the limit | P3-02 |
| A-08 | MEDIUM | Unhappy paths | **Session IDs are redacted before use.** `sessions.external_id` is the post-redaction value. If conversation IDs match an enabled preset (e.g. a user's email), all of them become `[REDACTED:email]` and unrelated conversations merge into one Session (REQ-036.a "unique per Project") | Data Model §2.8, TD DD-013, PRD REQ-036 | Decide in the spec: e.g. natural key = HMAC(instance secret, raw ID) computed before redaction, display value = redacted ID. Or document the merge as a known limitation | P3-03 |
| A-09 | MEDIUM | Data | **Rollup adoption path inconsistent.** Data Model §2.19 says rollups arrive via a later migration `0002_rollups` and that their approximate percentiles need a PRD delta. Plan P0-10 and Tasks P0-10.1 say "fold into `0001_initial`" and do not mention the PRD delta or the exact-equality contract test that rollups would break | Data Model §2.19, Plan P0-10, Tasks P0-10.1 | Align P0-10.1: adoption = Data Model v1.1 + a Delta Spec on REQ-106.c (approximate percentiles) + contract-test tolerance for rollup-backed percentiles | P0-10.1 |
| A-10 | MEDIUM | Terminology | **Kind enums differ between layers.** `spans.category` has 6 values (incl. `generic_genai`) vs. API `SpanKind` with 5; `graph_nodes.node_kind` (`agent`/`workflow`/`node`) vs. API `GraphNode.kind` (`agent`/`node`/`tool`/`llm`/`approval`). The mapping between them is not written down | Data Model §2.10/§2.14, API §6.1 | API v1.2: one mapping table (e.g. `generic_genai` → `generic` with an `is_genai` badge; `workflow` → `agent`; `tool`/`llm` only in `fallback_sequence` graphs) | P4-02 |
| A-11 | MEDIUM | Task executability | **Pricing seed ordering and source.** `0001_initial` loads `data/pricing.csv` (Data Model §5), which is produced in P3-05 — after P2-02 writes the migration. API §8.2 also has `[pricing] file` ("seed loaded as version 1 on an empty DB"), which a migration cannot see cleanly | Data Model §5, API §8.2, Plan P2-02/P3-05 | Move the seed load from the migration to the first-run bootstrap (it can honor `[pricing] file`), or move CSV generation before P2-02 | P2-02 |
| A-12 | MEDIUM | Ambiguity | **Auth-hook caching vs. the EARS criterion.** REQ-191.a says the hook is called "on each page load and event"; API §7.2 caches its result per client for 30 s. A test of REQ-191.a as written fails | PRD REQ-191.a, API §7.2 | PRD clarification ("the result may be cached up to 30 s per client") or drop the cache | P6-03 |
| A-13 | MEDIUM | Task executability | **[P] on tasks that share files.** P1-02.1, P1-02.2 and P1-02.3 all edit `pyproject.toml`; P0-05.1 and P0-05.2 share the spike boot script and Playwright test | Tasks P0/P1 | Remove those [P] markers (or give each tool its own config file) | P1-02.x |
| A-14 | MEDIUM | Coverage | **The DB canary for redaction comes only at the end.** REQ-322 (byte-level DB scan) is verified only in P10-01, although redaction is built in P3-04. A privacy gate failure would surface after the UI and Docker work | Plan P3-13, P10-01 | Add the SQLite canary scan to P3-13's Done; keep P10-01 as the full suite (PG dump, logs, self-telemetry) | P3-13 |
| A-15 | MEDIUM | Data | **FTS parity for identifiers is unverified.** Tool and span names such as `search_web` or `db.query` are common search terms. FTS5 `unicode61` and the PostgreSQL `simple` parser plus `to_tsquery` may split `_` and `.` differently, and DD-006 keeps `_` inside query tokens. REQ-073.b (same Run set on both backends) could fail on exactly these terms | TD DD-006, Data Model §2.15, PRD REQ-073.b | Specify one tokenization (split on `_`/`.` in both the document and the query builder) and add identifier cases to the contract dataset; check during S2 (P0-09.1) | P0-09.1 |
| A-16 | MEDIUM | Unhappy paths | **`import reflex` may do I/O itself.** REQ-190.c is verified with filesystem writes blocked. If importing Reflex (pulled in by `pages`) reads `rxconfig.py` or creates directories, the test fails for reasons outside our code | PRD REQ-190.c, Tasks P0 | Add to S1: import `reflex_agentlens.pages`-like modules with writes and sockets blocked and record what Reflex touches | P0-05.x |
| A-17 | MEDIUM | Unhappy paths | **Clock skew is unspecified.** Spans with timestamps in the future keep a Run `in_progress` (`last_span_ns > now − window`) and escape age-based retention; spans far in the past are purged on arrival | Data Model Q7/Q12/Q13, PRD REQ-032.b, 090 | Define a rule, e.g. reject or clamp spans with `start_ns > received_at + 1h` and record the case in `partialSuccess` | P3-02 |
| A-18 | LOW | Terminology | **Stale version references.** PRD, API and TD say "Governed by constitution v1.0" (current: v1.1); TD cites PRD v1.0 / API v1.0; Data Model cites TD v1.0 | All metadata tables | Bump the references in the next edit of each file | — |
| A-19 | LOW | Terminology | `api_key_id` (API §2) vs. `key_id` (API §3.4, Data Model); the Vocabulary's "Derived view" list omits GraphNode; TD §3.2 describes the Normalizer as "decode → validate → …" while Art. 4 requires validation *before* the normalizer (the code layout already separates `decode.py`/`validate.py`) | Constitution vocabulary, API §2, TD §3.2 | Use `key_id` everywhere; add GraphNode to the vocabulary; reword the TD row | — |
| A-20 | LOW | Coverage | REQ-210.b (UI + ingest within 30 s **on the reference machine**) is measured only in CI (P7-01), not on the reference machine | Plan P7-01, P8-05 | Add a cold-start measurement to the benchmark suite (P8-05) | P8-05 |
| A-21 | LOW | Ambiguity | Demo-mode ingest: REQ-231.b "disable ingest endpoints" vs. API §3.3 "403 `PERMISSION_DENIED`" (mounted but rejecting) | PRD REQ-231.b, API §3.3 | Pick one (suggest: not mounted → 404, consistent with "disabled") | P8-02 |
| A-22 | LOW | Ambiguity | TD §11 says S2 blocks "Phase 1 exit"; in the plan, S2 is P0 and gates P2 | TD §11, Plan | Reword when the S2 report is appended (P0-10.1) | — |
| A-23 | LOW | Unhappy paths | PRD §13 indicative timeline (~11–12 weeks) vs. plan estimate (~20 weeks on the critical path) | PRD §13, Plan §1 | Accept the plan as current, or update PRD §13 in its next revision | — |
| A-24 | LOW | Task executability | With empty `ingest`/`store`/`query` packages, coverage.py reports "No data to report" and exits non-zero, so the P1-02.3 gate would break CI during P1 | Tasks P1-02.3, P1-03.1 | State in P1-02.3 how the gate behaves before code exists (e.g. enforce `fail_under` once the packages contain modules) | P1-02.3 |
| A-25 | LOW | Coverage | Matrix "Tests" column is `pending` for every requirement. Expected under the rolling wave; the P1-05 script becomes the source of truth | Tasks matrix | Re-run Analyze at each phase revision of the Tasks file | — |

## Verdict

**FIX CRITICAL FIRST — scoped by phase.**

- Before the first batch (P0-01.1 …): **A-02**, **A-03**, plus the small task edits **A-13**, **A-15**
  (add to S2), **A-16** (add to S1).
- Before P2 starts: **A-01**, **A-06**, **A-09**, **A-11** (spec edits: TD/Data Model/API v1.2).
- Before P3 starts: **A-04**, **A-07**, **A-08**, **A-17**.
- Before P4: **A-05**, **A-10**. Before P6: **A-12**.
- LOW findings can be folded into the next edit of each artifact.

The Analyze presents findings only; approval is the maintainer's.

## Resolution log

| Finding | Status | Date | Resolution |
|---|---|---|---|
| A-02 | Resolved | 2026-10-31 | `specs/` committed on `docs/specs-v0.1` and proposed to `develop` via PR |
| A-03 | Resolved | 2026-10-31 | Tech Design v1.1.1 (DD-005 → `percentile_disc`); Tasks v1.0.1 (P0-09.2) |
| A-13 | Resolved | 2026-10-31 | Tasks v1.0.1: [P] removed from P0-05.1, P0-05.2, P1-02.1, P1-02.2, P1-02.3 |
| A-15 | Scheduled | 2026-10-31 | Tasks v1.0.1: new spike task P0-09.3; rule decided in the S2 report |
| A-16 | Scheduled | 2026-10-31 | Tasks v1.0.1: new spike task P0-05.3; scoping decided in the S1 report |
| Others | Open | — | Due before the phase named in the Verdict |

## Resolutions (2026-10-31, before P2)

| Finding | Resolution |
|---|---|
| A-01 | Tech Design v1.3 DD-004 concurrency rule; Data Model v1.1 Q6/Q7/Q12/Q13; multi-writer test P2-06.2 |
| A-06 | API v1.2 `get_span_raw` + `SpanRaw`; protocol-method ownership table in Tasks v1.1 (P2) |
| A-09 | Moot: rollups not adopted (Delta Spec `changes/2026-10-req-305-percentile-budget/`); Data Model §2.19 marked |
| A-11 | Data Model v1.1 §5: pricing seed moved from `0001_initial` to the first-run bootstrap |
| A-05 (storage side) | Protocol types are `store.records` dataclasses (API v1.2 §5); Pydantic DTO fields stay for P4 |
| A-04 | Tech Design v1.4 DD-003 per-batch savepoints; API v1.3 `write_group` / `BatchRejected` (400 / `INVALID_ARGUMENT`) |
| A-07 | Tech Design v1.4 Flow 1: redaction before truncation; canary test in P3 |
| A-08 | Data Model v1.2 §2.1 `session_key`, §2.8 HMAC-suffixed natural key when redaction changes a conversation ID; API v1.3 `get_or_create_secret` |
| A-17 | API v1.3 §3.6 `future_timestamp` (`ingest.max_clock_skew`, default 1 h) and `older_than_retention` |

