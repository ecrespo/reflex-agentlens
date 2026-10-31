# Delta — Separate latency budget for the dashboard percentile panel

Base: PRD v1.1, Tech Design v1.2, Data Model v1.0.1, Tasks v1.0.1. Evidence: Tech Design §11.2.

## MODIFIED

### specs/prd/agentlens-v0.1.md → REQ-305 — Dashboard latency · MUST
- **Before**:
  - **REQ-305.a** [U] THE SYSTEM SHALL compute the full dashboard aggregate set (REQ-106) for a
    30-day range in p95 < **1.5 s** on a 1M-span SQLite database and < **1 s** on a 1M-span
    PostgreSQL database. *This target decides the DuckDB question (§12, Q2).*
- **After**:
  - **REQ-305.a** [U] THE SYSTEM SHALL compute the dashboard aggregate set of REQ-106, **except
    the latency percentiles of REQ-106.c**, for a 30-day range in p95 < **1.5 s** on a 1M-span
    SQLite database and < **1 s** on a 1M-span PostgreSQL database.
  - **REQ-305.b** [U] THE SYSTEM SHALL compute the latency percentiles of REQ-106.c for a 30-day
    range in p95 < **3 s** on a 1M-span SQLite database and < **1 s** on a 1M-span PostgreSQL
    database, with exact nearest-rank values identical on both backends.
- **Reason**: spike S2: the full set took 3,327 ms on SQLite, of which 2,519 ms is Q10c; the rest
  takes ~0.8 s. PostgreSQL passes the whole set in 724 ms.

### specs/prd/agentlens-v0.1.md → §12 Q2 (DuckDB)
- **Before**: open, "Decided by REQ-305 … Adopt DuckDB only if plain SQLite misses the target in a
  spike benchmark".
- **After**: **Resolved (2026-10-31): no DuckDB in v0.1.** Spike S2 met REQ-305.a with plain SQL on
  both backends once the percentile panel has its own budget (REQ-305.b).

### specs/technical/agentlens-architecture.md → DD-005
- **After** (appended to Consequences): "Spike S2 outcome: the ladder is not used. REQ-305 is split
  (PRD v1.2): the latency-percentile panel (Q10c) has its own budget and is fetched by the
  dashboard as a separate query, so the other panels never wait for the ranking sort. Ladder step
  1 is reopened for Q10c only if the reference-machine benchmark (P8-06) misses REQ-305.b."
- §11.2 heading becomes "REQ-305 PASSED on both backends under PRD v1.2", and the open-decision
  note is replaced by a pointer to this change.

### specs/data-model/agentlens-schema.md → §2.19 and Q10
- §2.19 `rollup_hourly`: status "not adopted (spike S2, PRD v1.2 REQ-305.b)"; stays out of
  `0001_initial`.
- Q10c header gains `[REQ-106.c, 305.b]`; Q10a/b/d/e/f and Q8 cite `305.a`.

### specs/tasks/agentlens-tasks.md → P0-09.2, P0-10.1
- P0-09.2 Done is evaluated against REQ-305.a/.b (PRD v1.2): SQLite 808 ms / 2,519 ms ✓,
  PostgreSQL 364 ms / 360 ms ✓.
- P0-10.1: rollup not adopted; closed by this change.

## ADDED

### specs/prd/agentlens-v0.1.md → REQ-141 — Dashboard page
- **REQ-141.f** [E] WHEN the dashboard loads or refreshes, THE SYSTEM SHALL render every panel other
  than the latency chart (REQ-106.c) without waiting for the latency percentiles, and SHALL show a
  loading state in the latency chart until its data arrives.

## REMOVED

None.

## Version bumps on fold

PRD 1.1 → 1.2 · Tech Design 1.2 → 1.3 · Data Model 1.0.1 → 1.0.2 · Tasks 1.0.1 → 1.0.2.
