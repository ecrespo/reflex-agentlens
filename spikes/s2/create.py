# /// script
# requires-python = ">=3.11"
# dependencies = ["sqlalchemy[asyncio]>=2.0.36", "aiosqlite>=0.22.1", "asyncpg>=0.32.0"]
# ///
"""Spike S2a: create the hot tables on SQLite (WAL) and PostgreSQL (Task P0-07.1).

Usage: uv run --script create.py --sqlite PATH [--pg postgresql+asyncpg://...]
Prints a JSON report: backend versions, FTS5 availability, created tables and indexes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).parent))
import schema  # noqa: E402

PRAGMAS = (  # Data Model §2.0, every connection
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-65536",
)


def sqlite_engine(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        for p in PRAGMAS:
            cur.execute(p)
        cur.close()

    return engine


def describe(sync_conn) -> dict:
    insp = inspect(sync_conn)
    tables = sorted(insp.get_table_names())
    return {
        "tables": tables,
        "indexes": {t: sorted(i["name"] for i in insp.get_indexes(t)) for t in tables},
    }


async def run_sqlite(path: Path) -> dict:
    path.unlink(missing_ok=True)
    engine = sqlite_engine(path)
    async with engine.begin() as conn:
        # creation-time pragmas (§2.0): page_size/auto_vacuum before the first table, then WAL
        await conn.exec_driver_sql("PRAGMA page_size=8192")
        await conn.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
    async with engine.connect() as conn:
        mode = (await conn.exec_driver_sql("PRAGMA journal_mode=WAL")).scalar()
    async with engine.begin() as conn:
        await conn.run_sync(schema.metadata.create_all)
        await conn.run_sync(schema.create_search)
        version = (await conn.exec_driver_sql("select sqlite_version()")).scalar()
        opts = [r[0] for r in (await conn.exec_driver_sql("PRAGMA compile_options")).all()]
        page = (await conn.exec_driver_sql("PRAGMA page_size")).scalar()
        av = (await conn.exec_driver_sql("PRAGMA auto_vacuum")).scalar()
        fks = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        info = await conn.run_sync(describe)
        # FTS5 contentless_delete smoke: insert a doc, match it, delete it
        await conn.exec_driver_sql(
            "INSERT INTO span_search(rowid, doc) VALUES (1, 'Café weather_tool timeout')"
        )
        hit = (
            await conn.exec_driver_sql(
                "SELECT rowid FROM span_search WHERE span_search MATCH 'cafe'"
            )
        ).all()
        await conn.exec_driver_sql("DELETE FROM span_search WHERE rowid = 1")
        after = (
            await conn.exec_driver_sql(
                "SELECT count(*) FROM span_search WHERE span_search MATCH 'cafe'"
            )
        ).scalar()
        ddl_ck = (
            await conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'runs'")
        ).scalar()
    await engine.dispose()
    major, minor, *_ = (int(x) for x in version.split("."))
    return {
        "backend": "sqlite",
        "version": version,
        "version_ok_ge_3_43": (major, minor) >= (3, 43),
        "fts5": "ENABLE_FTS5" in opts,
        "journal_mode": mode,
        "page_size": page,
        "auto_vacuum": av,  # 2 = INCREMENTAL
        "foreign_keys": fks,
        "fts_match_diacritics_folded": [r[0] for r in hit] == [1],
        "fts_contentless_delete_ok": after == 0,
        "runs_ddl_has_len_checks": "length(status) <= 12" in ddl_ck,
        **info,
    }


async def run_pg(url: str) -> dict:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        await conn.exec_driver_sql("CREATE SCHEMA public")
        await conn.run_sync(schema.metadata.create_all)
        await conn.run_sync(schema.create_search)
        version = (await conn.exec_driver_sql("show server_version")).scalar()
        info = await conn.run_sync(describe)
        idx_defs = dict(
            (
                await conn.exec_driver_sql(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                    "('ix_llm_project_start','ix_spans_root_attrs','ix_runs_inprogress',"
                    "'ix_search_tsv')"
                )
            ).all()
        )
        ident = (
            await conn.exec_driver_sql(
                "SELECT is_identity, identity_generation FROM information_schema.columns "
                "WHERE table_name='spans' AND column_name='pk'"
            )
        ).one()
    await engine.dispose()
    return {
        "backend": "postgresql",
        "version": version,
        "spans_pk_identity": list(ident),
        "index_defs": idx_defs,
        **info,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", type=Path, required=True)
    ap.add_argument("--pg")
    args = ap.parse_args()
    report = [await run_sqlite(args.sqlite)]
    if args.pg:
        report.append(await run_pg(args.pg))
    print(json.dumps(report, indent=2))
    s = report[0]
    ok = s["version_ok_ge_3_43"] and s["fts5"] and s["journal_mode"] == "wal"
    ok = (
        ok
        and s["fts_contentless_delete_ok"]
        and set(report[-1]["tables"])
        >= {
            "sources",
            "resources",
            "sessions",
            "runs",
            "spans",
            "llm_calls",
            "tool_calls",
            "approvals",
            "graph_nodes",
            "span_search",
        }
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
