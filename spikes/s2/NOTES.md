# Spike S2 — SQL backends (throwaway branch `spike/s2-sql`, never merged)

Feeds the S2 report (P0-10.1). Code: `schema.py` (SQLAlchemy 2.0 Core, one MetaData for both
backends), `create.py` (PEP 723 script: `uv run --script spikes/s2/create.py --sqlite PATH --pg URL`).
PostgreSQL: `docker run -d --name agentlens-s2-pg -e POSTGRES_PASSWORD=spike -e POSTGRES_DB=agentlens
-p 127.0.0.1:55432:5432 postgres:18.6-trixie`.

## P0-07.1 — hot tables on both backends (2026-10-31) — PASSED

Result: `create-result.json`.

- `create_all` succeeds on **SQLite 3.51.0** (WAL, page_size 8192, auto_vacuum INCREMENTAL,
  foreign_keys ON) and **PostgreSQL 18.6**. Tables: projects, pricing_versions (FK targets, minimal),
  sources, resources, sessions, runs, spans, llm_calls, tool_calls, approvals, graph_nodes,
  span_search. 25 indexes on SQLite, 28 on PG (+GIN attrs, +GIN tsv, +search run index).
- SQLite: `ENABLE_FTS5` present, version ≥ 3.43 ✔. FTS5 `contentless_delete=1` works: insert, match
  `cafe` against `Café` (remove_diacritics 2), delete by rowid, no match after.
- §2.0 mapping expressed in one MetaData:
  - `PK`: `BigInteger().with_variant(Integer, "sqlite")` + `Identity(always=True)` → PG
    `GENERATED ALWAYS AS IDENTITY` (verified), SQLite `INTEGER … PRIMARY KEY(pk)` which **is** a rowid
    alias (verified `pk == rowid`). Identity is ignored by the SQLite dialect.
  - SQLite-only CHECKs (`length(col) <= n`, `json_valid`) and per-backend indexes
    (`INCLUDE` vs. appended covering columns; PG partial GIN) via `Constraint/Index.ddl_if(dialect=…)`.
    Two `Index` objects with the same name and opposite `ddl_if` coexist without complaint.
  - `BYTES(n)` CHECK uses `length(col) = n` on both backends (PG `length(bytea)` counts bytes), so no
    per-dialect variant is needed.
  - `Boolean(create_constraint=True)` gives the `IN (0,1)` CHECK on SQLite and native BOOLEAN on PG.
    Constraint names must be table-unique; the spike reuses `ck_bool_<col>` across tables, which is
    legal on both but the real schema should use a naming convention (`MetaData(naming_convention=…)`).
- `span_search` cannot share the MetaData (virtual table vs. TSVECTOR table): created by a separate
  `create_search(conn)` step. **FTS5 creates shadow tables** (`span_search_config/_data/_docsize/_idx`):
  Alembic autogenerate (P2) must exclude `span_search*` via `include_object`, and the SQLite span_search
  + delete trigger go in a hand-written migration op.
- Partial index predicate on PG is normalized to `((status)::text = 'in_progress'::text)`; queries
  must use the literal predicate for the planner to pick it (check in P0-09).
- Spec gap (minor, for Data Model v1.0.1): §2.0 says `TEXT(n)` → `VARCHAR(n)` on PG but SQLAlchemy
  `String(n)` renders `VARCHAR(n)` on SQLite too (TEXT affinity, harmless). No change needed.

Machine for every S2 number: AMD Ryzen AI 9 HX PRO 370 (24 threads), 45 GB RAM, NVMe, Linux 7.0;
Python 3.13.9, SQLAlchemy 2.0 async (aiosqlite / asyncpg), SQLite 3.51.0, PostgreSQL 18.6 in
Docker (default config, loopback TCP). Single writer, one Project.

## P0-07.2 — synthetic dataset generator (2026-10-31) — PASSED

`gen.py` (seeded `random.Random`), loaded through the real write path (`writer.write_group`,
5,000-span groups) by `load.py`. Results: `results/load-100k.json`, `results/load-1m.json`.

| Dataset | Runs | Sessions | Spans | Checksum (same seed twice) | SQLite load | PG load |
|---|---|---|---|---|---|---|
| 100k (+1 trace of 1,009 spans) | 4,684 | 865 | 100,016 | `af19bbda…` identical | 9.8 s, 85 MB | 13.6 s, 162 MB |
| 1M | 47,127 | 8,669 | 1,000,014 | `3d2045a3…` identical | 56.9 s (17.6k spans/s), 843 MB | 113.9 s (8.8k spans/s), 1.47 GB |

Row counts are identical on both backends (llm_calls 377,387; tool_calls 188,590; graph_nodes
235,836; approvals 9,492 at 1M). Size ≈ 0.84 KB/span (SQLite) and 1.47 KB/span (PG) with ~0.5 KB of
message text per LLM span, in line with the Data Model's 1–2 GB/1M estimate.

## P0-08.2 — order invariance (2026-10-31) — PASSED

`order.py`: the same 10k spans (470 runs, 80 sessions) in generation order, reversed (512-span
batches), shuffled into random batch sizes (1–1,500), and shuffled plus a 10 % duplicate re-send.
`runs` and `sessions` row hashes (natural keys only) are identical across all four orders **and across
the two backends** (`cd81043f…` / `0d859b03…` on both). Duplicates are dropped by
`ON CONFLICT DO NOTHING RETURNING` and do not change any aggregate (REQ-038). Result:
`results/order.json`.

Q7 as implemented (`writer._q7`): one `WITH … UPDATE runs … FROM calc` statement per group;
`ROW_NUMBER()` windows instead of `DISTINCT ON` so the same SQL text works on both backends; the
affected set is `unnest(CAST(:pks AS BIGINT[]))` on PG and `json_each(:pks)` on SQLite (no temp
table needed); `models` uses `jsonb_agg(DISTINCT … ORDER BY …)` vs. `json_group_array` over an
ordered DISTINCT subquery. Sessions are recomputed for old ∪ new session pks of the affected runs.
- asyncpg cannot infer the type of `:now_ns - :window_ns` (`operator is not unique: unknown -
  unknown`): compute derived bind values in Python (`:cutoff_ns`). Rule for `store.dialect`.

## P0-09.3 — full-text parity on identifiers (2026-10-31) — FAILED as specified, fix proposed

`fts.py`, result `results/fts.json`. With DD-006 as written (raw document text; tokens
`[\p{L}\p{N}_]+`), 5 of 18 terms differ:

| Cause | FTS5 `unicode61 remove_diacritics 2` | PG `to_tsvector('simple')` |
|---|---|---|
| `db.query`, `sql.execute` in documents | split: `db`, `query` | kept as one *host/file* token `db.query` → `db`, `query` don't match |
| `café`, `Résumé` | folded to `cafe`, `resume` | kept with accents → `cafe`/`resume` don't match |
| `search_web` | `search`,`web` (phrase query) | `search`,`web` (`<->` phrase) — same result |
| `gpt-4o-mini` | `gpt`,`4o`,`mini` | `gpt-4o-mini` + parts — same result for AND queries |

**Proposed DD-006 rule (verified: 18/18 identical):** the ingest normalizer emits the search document
already normalized — NFKD, drop combining marks, lowercase, every run of non-letter/digit characters
(including `_ . -`) replaced by one space — and the query builder applies the same function to the
user's input, then ANDs the tokens (FTS5 `"t1" "t2"`, PG `to_tsquery('simple', 't1 & t2')`). Both
backends then see plain space-separated words, so their parsers agree. Side effect: `search_web` is
two AND-ed words (matches "web search" too); acceptable for v0.1, or the builder can turn one
multi-token user term into a phrase (`"search web"` / `search <-> web`) — both backends support it.

## P0-08.1 — write-path throughput (2026-10-31) — PASSED (indicative)

`bench_write.py`: 4 producers × 512-span batches → bounded queue → 1 writer, groups ≤ 50 ms or
≤ 5,000 spans, each group = one transaction with Q6 + Q7 + session recompute + derived rows + search
docs. 120 s per backend, fresh database. Result: `results/write-throughput.json`.

| Backend | spans/s | Groups | Group size p50 | Commit p50 | Commit p95 | Commit max |
|---|---|---|---|---|---|---|
| SQLite (WAL, `synchronous=NORMAL`) | **16,002** | 374 | 5,120 | 262 ms | 526 ms | 825 ms |
| PostgreSQL 18 (Docker, defaults) | **7,493** | 175 | 5,120 | 565 ms | 1,327 ms | 3,267 ms |

The writer is saturated (groups always hit the 5,000-span cap), so these are maximum sustained
rates for one in-process writer with producers on the same event loop. Compare with REQ-300.a in
P0-10.1. PG commit time grows with table size (no tuning, no `synchronous_commit=off`); the max of
3.3 s should be re-measured with a tuned container before drawing conclusions.

## P0-09.1 — Runs and Trace query latency, 100k (2026-10-31) — PASSED

`queries.py --suite runs`, 50 timed runs after warm-up. Result: `results/queries-runs-100k.json`,
plans in `results/explain-runs-*.txt`.

| Query | SQLite p95 | PG p95 | Target |
|---|---|---|---|
| Q1 default page (51 rows) | 1.0 ms | 0.3 ms | < 300 ms (REQ-303) |
| Q1 keyset page 2 | 0.8 | 0.9 | |
| Q2 model / tool / status / duration | 0.4 / 0.6 / 0.6 / 3.2 | 1.0 / 1.5 / 0.3 / 1.1 | |
| Q3 root-attribute eq / FTS two terms | 0.5 / 1.9 | 0.6 / 7.0 | |
| Q5 trace of 1,009 spans (all LEFT JOINs) | 8.6 | 9.6 | < 500 ms (REQ-304) |

Rows match across backends except surrogate keys: **PG identity values are consumed by
`INSERT … ON CONFLICT DO NOTHING`** (run stubs re-sent every group), so `runs.pk` has gaps and differs
from SQLite. Harmless (pks never leave the store, keyset order is `(start_ns, pk)`), but contract
tests must compare natural keys, never pks.

## P0-09.2 — Dashboard aggregates, 1M spans / 30 days (2026-10-31) — PG PASSED, SQLite MISSED

`queries.py --suite dash`. Results: `results/queries-dash-1m-first.json` (first run, three
separate percentile windows) and `results/queries-dash-1m.json` (with the one-pass variant).
**All aggregate values are identical on both backends** (KPIs, buckets, sums, every p50/p95/p99).

| Query | SQLite p95 | PG p95 |
|---|---|---|
| Q8 KPIs (incl. p95 run duration) | 104 ms | 84 ms |
| Q10a runs by status/day | 29 | 11 |
| Q10b tokens/cost by model; by model/day | 429; 252 | 221; 200 |
| Q10c latency p50/p95/p99, 3 windows (first run) | 9,070 | 507 |
| **Q10c one pass** (PG `percentile_disc(ARRAY[…])`, SQLite one window per stream) | **2,519** | **360** |
| Q10d top tools; Q10e by source; Q10f approvals | 70; 35; 16 | 32; 23; 3 |
| **Full set (with Q10c one pass)** | **3,327 ms** ✗ (< 1,500) | **724 ms** ✓ (< 1,000) |

- Q10c ranks ~850k rows (all LLM + tool + graph-node rows in 30 days). On SQLite that costs
  ~2.5 s even in one pass; everything else in the set costs ~0.8 s.
- Nearest-rank parity detail: PG computes the rank as `ceil(p * N)` in **double precision**
  (`0.95 * 20 = 19.000000000000004 → 20`). The SQLite builder reproduces the same double arithmetic
  (`CAST(x AS INTEGER) + (x > CAST(x AS INTEGER))`), which is why results are identical. The
  Data Model's "⌈p·N⌉" must say "computed in IEEE double, as PostgreSQL does".
- **Decision needed in P0-10.1** (not taken here; it changes the PRD): see the options in the
  S2 summary. Ladder step 1 (hourly rollups with log2 histograms) makes percentiles approximate,
  which the PRD allows only through a Delta Spec.
