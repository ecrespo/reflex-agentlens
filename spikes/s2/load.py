# /// script
# requires-python = ">=3.11"
# dependencies = ["sqlalchemy[asyncio]>=2.0.36", "aiosqlite>=0.22.1", "asyncpg>=0.32.0"]
# ///
"""Spike S2a: generate a seeded dataset and load it through the write path (Task P0-07.2).

Usage: uv run --script load.py --workdir DIR --spans 1000000 [--big-trace 1000] [--backends ...]
Loads in 5,000-span groups (the group-commit upper bound of DD-003), sequentially.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sqlalchemy import text  # noqa: E402

import gen  # noqa: E402
import writer  # noqa: E402

COUNT_TABLES = ("runs", "sessions", "spans", "llm_calls", "tool_calls", "approvals", "graph_nodes")


async def load(backend: str, name: str, workdir: Path, spans: list, group: int) -> dict:
    engine = await writer.fresh(backend, name, workdir)
    caches = writer.Caches()
    now = gen.BASE_NS + 40 * 86_400 * gen.NS
    t0 = time.perf_counter()
    for i in range(0, len(spans), group):
        async with engine.begin() as conn:
            await writer.write_group(conn, spans[i : i + group], now, caches)
    elapsed = time.perf_counter() - t0
    async with engine.begin() as conn:
        if backend == "sqlite":
            await conn.exec_driver_sql("ANALYZE")
            await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            await conn.exec_driver_sql("ANALYZE")
        counts = {
            t: (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar()  # noqa: S608
            for t in COUNT_TABLES
        }
        if backend == "sqlite":
            size = (workdir / f"{name}.db").stat().st_size
        else:
            size = (
                await conn.execute(text("SELECT pg_database_size(current_database())"))
            ).scalar()
    await engine.dispose()
    return {
        "load_s": round(elapsed, 1),
        "spans_per_s": round(len(spans) / elapsed),
        "db_bytes": size,
        "counts": counts,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--spans", type=int, required=True)
    ap.add_argument("--big-trace", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--backends", default="sqlite,postgresql")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()
    t0 = time.perf_counter()
    runs = gen.dataset(args.seed, args.spans, args.big_trace)
    gen_s = time.perf_counter() - t0
    # determinism: a second generation with the same seed must match exactly
    again = gen.dataset(args.seed, args.spans, args.big_trace)
    spans = [s for r in runs for s in r]
    report = {
        "seed": args.seed,
        "spans": len(spans),
        "runs": len(runs),
        "sessions": len({r[0].session_ext for r in runs}),
        "checksum": gen.checksum(runs),
        "deterministic": gen.checksum(again) == gen.checksum(runs) and len(again) == len(runs),
        "generate_s": round(gen_s, 1),
    }
    del again
    name = args.name or f"s2_{args.spans // 1000}k"
    for backend in args.backends.split(","):
        report[backend] = await load(backend, name, args.workdir, spans, 5_000)
        print(json.dumps({backend: report[backend]}), file=sys.stderr, flush=True)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
