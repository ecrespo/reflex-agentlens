# Proposal — Separate latency budget for the dashboard percentile panel (REQ-305)

> Status: `IN REVIEW` (adopted provisionally under the maintainer's "automatic approval" of
> 2026-10-31; folding into `specs/` waits for explicit approval of this PR)
> Base specs affected: `specs/prd/agentlens-v0.1.md` (REQ-305, REQ-141, §12 Q2),
> `specs/technical/agentlens-architecture.md` (DD-005, §11.2),
> `specs/data-model/agentlens-schema.md` (§2.19, Q10), `specs/tasks/agentlens-tasks.md` (P0-09.2, P0-10.1)
> Date: 2026-10-31 · Author: Claude (for Ernesto Crespo) · Closes: Tasks P0-10.1

## Problem

Spike S2 (Tech Design §11.2) measured the full dashboard aggregate set (Q8 + Q10a–f) on 1M spans
over 30 days:

| Backend | Full set p95 | Of which Q10c (latency p50/p95/p99) | Rest of the set | REQ-305.a target |
|---|---|---|---|---|
| SQLite 3.51 | 3,327 ms ✗ | 2,519 ms | ~810 ms | < 1,500 ms |
| PostgreSQL 18.6 | 724 ms ✓ | 360 ms | ~364 ms | < 1,000 ms |

The miss is entirely the latency-percentile panel (REQ-106.c): exact nearest-rank percentiles need
a sort of ~850k rows per range, and SQLite has no `percentile_disc`. Every other aggregate is
well inside budget on both backends, and every value is identical across backends.

## Options considered

| Option | Effect | Cost |
|---|---|---|
| **(a) Separate budget for the percentile panel** (chosen) | Exact percentiles, exact backend parity kept; the rest of the dashboard renders within the original budget, the percentile panel arrives later | PRD change (this delta); UI loads the panel as its own request |
| (b) DD-005 ladder step 1: `rollup_hourly` with log2 histograms | Dashboard fast on SQLite | Percentiles become approximate (bucket error up to 2×), a second write-path table, histogram parity rules, a migration |
| (c) DD-005 ladder step 2: DuckDB | Fast scans | Large native dependency, a second dialect, file-locking against the live SQLite writer, nothing for PostgreSQL (Constitution Art. 2) |

(a) is the smallest change that keeps every number correct. The cost is UX: on a large SQLite
database, the latency chart appears up to ~2–3 s after the other panels. That is acceptable for a
self-hosted single-node tool, and PostgreSQL deployments (the recommended topology at this size)
are unaffected.

## Scope

Changes: REQ-305 split into .a (dashboard without the latency-percentile panel) and .b (latency
panel alone); new REQ-141.f (the panel loads independently and shows a loading state); PRD Q2
closed (no DuckDB); DD-005 records the outcome; Data Model §2.19 `rollup_hourly` stays out of
`0001_initial` (not adopted).

Does **not** change: what REQ-106 computes, the nearest-rank definition, exact backend parity,
the KPI p95 run duration (REQ-106.g, computed over `runs`, already inside the 0.8 s), the
REQ-141.d refresh rule.

## Impact

- No wire-protocol change: dashboard aggregates are an in-process `QueryService` call returning
  `DashboardData` (Data Model DTO table); the latency series becomes a separate call
  (`dashboard_latency`) so the page can fetch it in its own background event. The API Spec gains that method name when
  P5 (query) is detailed.
- No migration: the schema is unchanged.
- Benchmarks (P8-06) measure REQ-305.a and REQ-305.b separately.
- Risk: the reference machine (P8-06) may be slower than the spike machine. REQ-305.b leaves
  ~20 % headroom over the measured 2.5 s; if P8-06 misses it, ladder step 1 is reopened for
  the percentile panel only.

## Constitution check

- Art. 2 (minimal dependencies): no new dependency. ✓
- Art. 6 (aggregates in SQL): unchanged, percentiles stay in SQL. ✓
- Art. 8 (testing): REQ-305.a/.b each get a benchmark assertion; REQ-141.f a UI test. ✓
- Art. 10 (process): change goes through this Delta Spec; REQ IDs are kept, one sub-criterion added. ✓
