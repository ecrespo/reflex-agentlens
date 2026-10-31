"""Dialect operation tests: plan task P2-03.1 (DD-005, DD-006, REQ-325.b, REQ-073.b)."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Connection, Engine, column, select, table, text
from sqlalchemy.ext.asyncio import AsyncEngine

from reflex_agentlens.core.search import search_normalize, search_tokens
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.dialect import PostgresOps, SqliteOps, ops_for, percentile_rank

DOCS = {
    1: "execute_tool search_web best pizza near me",
    2: "execute_tool db.query select * from orders",
    3: "chat gpt-4o-mini summarize the invoice",
    4: "execute_tool RefundTool refund order 991",
    5: "web search for café opening hours",
    6: 'he said "drop table" OR maybe NEAR(x) not',
    7: "Résumé naïve CAFÉ",
}
HOSTILE = [
    '"',
    '""',
    "'",
    "' OR 1=1 --",
    '" OR "a',
    "search*",
    "^search",
    "doc:search",
    "NEAR(search web)",
    "search AND NOT web",
    "a) OR (b",
    "drop table; --",
    "café & résumé | !x",
    "\\",
    "\x00search",
    "",
    "   ",
]


def _expected(query: str) -> set[int]:
    tokens = search_tokens(query)
    return {
        pk for pk, doc in DOCS.items() if set(tokens) <= set(search_normalize(doc).split())
    } if tokens else set()  # fmt: skip


def _load_docs(conn: Connection) -> None:
    ops = ops_for(conn.dialect.name)
    if conn.dialect.name == "postgresql":
        conn.execute(text("SET session_replication_role = replica"))  # skip the FK to spans
    for pk, doc in DOCS.items():
        conn.execute(
            ops.search_insert(),
            {"pk": pk, "run_pk": 1, "project_pk": 1, "doc": search_normalize(doc)},
        )


def _search(conn: Connection, query: str) -> set[int] | None:
    ops = ops_for(conn.dialect.name)
    fts = ops.fts_query(query)
    if fts is None:
        return None
    key = "rowid" if conn.dialect.name == "sqlite" else "span_pk"
    rows = conn.execute(text(f"SELECT {key} FROM span_search WHERE {fts.sql}"), fts.params)  # noqa: S608
    return {r[0] for r in rows}


@pytest.fixture
def fts_db(sqlite_engine: Engine) -> Connection:
    conn = sqlite_engine.connect()
    s.create_all(conn)
    _load_docs(conn)
    return conn


@pytest.mark.parametrize("query", [*HOSTILE, "search web", "db.query", "gpt-4o-mini", "CAFE"])
def test_fts_user_input_never_reaches_sql_syntax_sqlite(fts_db: Connection, query: str) -> None:
    """REQ-325.b: quotes and FTS operators are data; results equal the AND of the terms."""
    result = _search(fts_db, query)
    assert result is None if not search_tokens(query) else result == _expected(query)


def test_fts_sql_text_is_constant_for_any_input() -> None:
    """REQ-325.b: the query string is bound; the SQL text never varies with the input."""
    for ops in (SqliteOps(), PostgresOps()):
        texts = {q.sql for q in map(ops.fts_query, HOSTILE) if q is not None}
        assert len(texts) == 1


def test_fts_identifier_queries_match_across_tokenization() -> None:
    """REQ-073.b, DD-006: identifiers match after normalization on the SQLite side."""
    assert SqliteOps().fts_query("db.query") is not None
    assert PostgresOps().fts_query("db.query").params == {"fts_q": "'db' & 'query'"}  # type: ignore[union-attr]
    assert SqliteOps().fts_query("db.query").params == {"fts_q": '"db" "query"'}  # type: ignore[union-attr]


@pytest.mark.pg
async def test_fts_hostile_inputs_on_postgres(pg_engine: AsyncEngine) -> None:
    """REQ-325.b and REQ-073.b on PostgreSQL: same result sets as SQLite for every query."""
    async with pg_engine.begin() as conn:
        await conn.run_sync(s.create_all)
        await conn.run_sync(_load_docs)
        for query in [*HOSTILE, "search web", "db.query", "gpt-4o-mini", "CAFE"]:
            result = await conn.run_sync(_search, query)
            assert result is None if not search_tokens(query) else result == _expected(query)


# ── percentiles ──────────────────────────────────────────────────────────────────────────────


def _reference(rows: Sequence[tuple[str, int]], ps: Sequence[float]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for g, v in rows:
        groups[g].append(v)
    return {
        g: [sorted(vs)[percentile_rank(p, len(vs)) - 1] for p in ps] for g, vs in groups.items()
    }


def _percentiles(
    conn: Connection, rows: Sequence[tuple[str, int]], ps: Sequence[float]
) -> dict[str, list[int]]:
    conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS pv (g TEXT, d BIGINT)"))
    conn.execute(text("DELETE FROM pv"))
    if rows:
        conn.execute(
            text("INSERT INTO pv (g, d) VALUES (:g, :d)"), [{"g": g, "d": d} for g, d in rows]
        )
    sql = ops_for(conn.dialect.name).percentile_sql(ps, "d", ["g"], ["SELECT g, d FROM pv"])
    return {r[0]: list(r[1:]) for r in conn.execute(text(sql))}


PS = (0.5, 0.95, 0.99)
pct_rows = st.lists(st.tuples(st.sampled_from("abc"), st.integers(-(2**40), 2**40)), max_size=60)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pct_rows)
def test_percentile_sql_matches_nearest_rank_reference_sqlite(
    sqlite_engine: Engine, rows: list[tuple[str, int]]
) -> None:
    """DD-005 / Data Model Q8: percentile_disc semantics, rank computed in IEEE double."""
    with sqlite_engine.connect() as conn:
        assert _percentiles(conn, rows, PS) == _reference(rows, PS)


def test_rank_uses_double_precision_like_postgres() -> None:
    """Data Model Q8: ⌈p·n⌉ in IEEE double, as PostgreSQL's percentile_disc (checked on PG 18)."""
    assert 0.07 * 100 > 7  # 7.000000000000001
    assert percentile_rank(0.07, 100) == 8  # PostgreSQL returns the 8th value, not the 7th
    assert percentile_rank(0.95, 20) == 19  # 0.95 * 20 is exactly 19.0 in double
    assert percentile_rank(0.57, 100) == 57  # 56.99999999999999
    assert percentile_rank(0.5, 1) == 1
    assert percentile_rank(1.0, 7) == 7


def test_double_precision_edge_case_in_sql(sqlite_engine: Engine) -> None:
    rows = [("a", v) for v in range(1, 101)]
    with sqlite_engine.connect() as conn:
        assert _percentiles(conn, rows, (0.07, 0.57)) == {"a": [8, 57]}


def test_ungrouped_and_multi_source_percentiles(sqlite_engine: Engine) -> None:
    ops = SqliteOps()
    with sqlite_engine.connect() as conn:
        sql = ops.percentile_sql(
            (0.5,), "d", [], ["SELECT 3 AS d UNION ALL SELECT 1 UNION ALL SELECT 2"]
        )
        assert conn.execute(text(sql)).scalar_one() == 2
        sql = ops.percentile_sql(
            (1.0,), "d", ["g"], ["SELECT 'x' AS g, 5 AS d", "SELECT 'y' AS g, 9 AS d"]
        )
        assert sorted(conn.execute(text(sql)).all()) == [("x", 5), ("y", 9)]


@pytest.mark.parametrize("bad", [0.0, 1.5, -0.1, 1, math.nan, "0.5"])
def test_invalid_percentiles_are_rejected(bad: Any) -> None:
    with pytest.raises(ValueError, match="percentile"):
        SqliteOps().percentile_sql([bad], "d", [], ["SELECT 1 AS d"])
    with pytest.raises(ValueError, match="percentile"):
        PostgresOps().percentile_sql([bad], "d", [], ["SELECT 1 AS d"])
    with pytest.raises(ValueError, match="at least one"):
        SqliteOps().percentile_sql([], "d", [], ["SELECT 1 AS d"])


@pytest.mark.pg
async def test_percentile_sql_matches_reference_postgres(pg_engine: AsyncEngine) -> None:
    """DD-005: identical nearest-rank results on PostgreSQL (percentile_disc)."""
    import random

    rng = random.Random(7)  # noqa: S311 - test data
    datasets = [
        [("a", v) for v in range(1, 101)],
        [(rng.choice("abc"), rng.randint(-1000, 1000)) for _ in range(500)],
        [("z", 42)],
    ]
    async with pg_engine.connect() as conn:
        for rows in datasets:
            got = await conn.run_sync(_percentiles, rows, (*PS, 0.07, 0.57))
            assert got == _reference(rows, (*PS, 0.07, 0.57))


# ── JSON and id sets ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["gen_ai.request.model", 'we"ird', "back\\slash", "x') --"])
def test_json_get_binds_the_key_sqlite(sqlite_engine: Engine, key: str) -> None:
    """REQ-325.b: attribute keys are bound parameters, including hostile ones."""
    doc = table("t", column("attrs"))
    with sqlite_engine.connect() as conn:
        conn.execute(text("CREATE TEMP TABLE t (attrs TEXT)"))
        conn.execute(text("INSERT INTO t VALUES (json_object(:k, 'v'))"), {"k": key})
        expr = SqliteOps().json_get(doc.c.attrs, key)
        assert key not in str(expr.compile(conn))
        assert conn.execute(select(expr).select_from(doc)).scalar_one() == "v"


@pytest.mark.pg
async def test_json_get_binds_the_key_postgres(pg_engine: AsyncEngine) -> None:
    doc = table("t", column("attrs"))
    async with pg_engine.connect() as conn:
        await conn.execute(text("CREATE TEMP TABLE t (attrs JSONB)"))
        for key in ("gen_ai.request.model", 'we"ird', "x') --"):
            await conn.execute(text("DELETE FROM t"))
            await conn.execute(
                text("INSERT INTO t VALUES (jsonb_build_object(CAST(:k AS TEXT), 'v'))"), {"k": key}
            )
            assert (
                await conn.execute(select(PostgresOps().json_get(doc.c.attrs, key)))
            ).scalar_one() == "v"


def test_id_set_round_trips_on_sqlite(sqlite_engine: Engine) -> None:
    ops = SqliteOps()
    with sqlite_engine.connect() as conn:
        rows = conn.execute(text(ops.id_set_sql("ids")), {"ids": ops.id_set_value([3, 1, 2])})
        assert sorted(r[0] for r in rows) == [1, 2, 3]


def test_ops_for_rejects_unknown_dialects() -> None:
    assert ops_for("sqlite").name == "sqlite"
    with pytest.raises(ValueError, match="unsupported"):
        ops_for("mysql")
