"""Schema tests: plan tasks P2-01.1 and P2-01.2 (Data Model §2, §3)."""

from __future__ import annotations

import pytest
from seed import NS, PROJECT_TABLES, counts, seed_project
from sqlalchemy import Connection, Engine, delete, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from reflex_agentlens.store import schema as s

TABLES = {
    "app_meta", "projects", "api_keys", "redaction_rule_sets", "pricing_versions",
    "pricing_entries", "sources", "resources", "sessions", "runs", "spans", "llm_calls",
    "tool_calls", "approvals", "graph_nodes", "span_search", "metric_points", "saved_filters",
    "purge_log",
}  # fmt: skip

# Every index named in Data Model §2 (v1.1).
DATA_MODEL_INDEXES = {
    "uq_projects_id", "uq_projects_slug", "uq_api_keys_key_id", "ix_api_keys_project",
    "uq_pricing_entry", "uq_sources", "uq_resources", "uq_sessions_id", "uq_sessions_ext",
    "ix_sessions_project_last", "uq_runs_trace", "ix_runs_project_start",
    "ix_runs_project_status_start", "ix_runs_project_source_start", "ix_runs_session_start",
    "ix_runs_project_last", "ix_runs_inprogress", "uq_spans_run_span", "ix_spans_root_attrs",
    "ix_llm_project_start", "ix_llm_project_model_run", "ix_llm_run", "ix_tool_project_start",
    "ix_tool_project_name_run", "ix_tool_run", "ix_appr_project_requested", "ix_appr_run",
    "ix_gn_project_start", "ix_gn_run", "ix_search_tsv", "ix_search_run", "uq_metric_point",
    "ix_mp_project_name_time", "uq_saved_filters_id", "uq_saved_filters_name",
}  # fmt: skip
PG_ONLY_INDEXES = {"ix_spans_root_attrs", "ix_search_tsv", "ix_search_run"}


def _index_names(conn: Connection) -> set[str]:
    insp = inspect(conn)
    return {
        ix["name"]
        for table in insp.get_table_names()
        if not table.startswith("span_search_")  # FTS5 shadow tables
        for ix in insp.get_indexes(table)
        if ix["name"]
    }


def _tables(conn: Connection) -> set[str]:
    return {t for t in inspect(conn).get_table_names() if not t.startswith("span_search_")}


# ── SQLite ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(sqlite_engine: Engine) -> Connection:
    conn = sqlite_engine.connect()
    s.create_all(conn)
    conn.commit()
    return conn


def test_create_all_sqlite_creates_every_table(db: Connection) -> None:
    assert _tables(db) == TABLES


def test_sqlite_indexes_match_data_model(db: Connection) -> None:
    assert _index_names(db) == DATA_MODEL_INDEXES - PG_ONLY_INDEXES


def test_project_delete_cascades_to_every_dependent_table_sqlite(db: Connection) -> None:
    """REQ-170.d (storage side): deleting a Project removes every row that belongs to it."""
    keep = seed_project(db, slug="keep", n=2)
    gone = seed_project(db, slug="gone", n=1)
    before = counts(db)
    assert all(v == 2 for v in before.values()), before

    db.execute(delete(s.projects).where(s.projects.c.pk == gone))

    assert counts(db) == dict.fromkeys((*PROJECT_TABLES, "span_search"), 1)
    assert db.execute(text("SELECT count(*) FROM pricing_versions")).scalar_one() == 1
    assert db.execute(text("SELECT pk FROM projects")).scalars().all() == [keep]


@pytest.mark.parametrize(
    "values",
    [
        {"slug": "x" * 41},  # TEXT(40)
        {"content_storage": 2},  # BOOL
        {"retention_max_age_s": -1},
        {"request_rate": 0},
    ],
)
def test_sqlite_checks_reject_invalid_project_rows(
    db: Connection, values: dict[str, object]
) -> None:
    row = {"id": "01J" + "0" * 23, "slug": "p", "name": "p", "created_at_ns": NS,
           "updated_at_ns": NS} | values  # fmt: skip
    cols = ", ".join(row)
    binds = ", ".join(f":{c}" for c in row)
    with pytest.raises(IntegrityError):  # raw SQL: bypass SQLAlchemy's own type coercion
        db.execute(text(f"INSERT INTO projects ({cols}) VALUES ({binds})"), row)  # noqa: S608


@pytest.mark.parametrize(
    ("extra_col", "trace_id", "extra_val"),
    [
        ("usage_source", "x'00'", "'none'"),  # BYTES(16): a 1-byte trace ID
        ("status", "zeroblob(16)", "'done'"),  # enum CHECK
        ("models", "zeroblob(16)", "'{not json'"),  # json_valid CHECK
    ],
)
def test_sqlite_checks_reject_bad_bytes_enum_and_json(
    db: Connection, extra_col: str, trace_id: str, extra_val: str
) -> None:
    project_pk = seed_project(db)
    sql = (
        "INSERT INTO runs (project_pk, start_ns, last_span_ns, updated_at_ns, trace_id, "  # noqa: S608
        f"{extra_col}) VALUES ({project_pk}, 1, 1, 1, {trace_id}, {extra_val})"
    )
    with pytest.raises(IntegrityError):  # raw SQL: the constants above are test literals
        db.execute(text(sql))


def test_sqlite_fk_restricts_source_delete_while_referenced(db: Connection) -> None:
    seed_project(db)
    with pytest.raises(IntegrityError):
        db.execute(delete(s.sources))


def test_session_delete_sets_run_session_null(db: Connection) -> None:
    seed_project(db)
    db.execute(delete(s.sessions))
    assert db.execute(text("SELECT session_pk FROM runs")).scalar_one() is None


def test_span_delete_removes_search_document_sqlite(db: Connection) -> None:
    seed_project(db)
    db.execute(delete(s.runs))
    assert counts(db)["span_search"] == 0
    assert counts(db)["llm_calls"] == 0


# ── PostgreSQL ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.pg
async def test_create_all_pg_tables_and_indexes(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.run_sync(s.create_all)
        assert await conn.run_sync(_tables) == TABLES
        assert await conn.run_sync(_index_names) == DATA_MODEL_INDEXES


@pytest.mark.pg
async def test_project_delete_cascades_to_every_dependent_table_pg(
    pg_engine: AsyncEngine,
) -> None:
    """REQ-170.d (storage side) on PostgreSQL, including the ``span_search`` FK cascade."""
    async with pg_engine.begin() as conn:
        await conn.run_sync(s.create_all)
        await conn.run_sync(lambda c: seed_project(c, slug="keep", n=2))
        gone = await conn.run_sync(lambda c: seed_project(c, slug="gone", n=1))
        await conn.execute(delete(s.projects).where(s.projects.c.pk == gone))
        assert await conn.run_sync(counts) == dict.fromkeys((*PROJECT_TABLES, "span_search"), 1)
