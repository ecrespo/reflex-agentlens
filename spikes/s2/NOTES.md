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
