# /// script
# requires-python = ">=3.11"
# dependencies = ["sqlalchemy[asyncio]>=2.0.36", "aiosqlite>=0.22.1", "asyncpg>=0.32.0"]
# ///
"""Spike S2b: order-invariance of the Run/Session recompute (Task P0-08.2, REQ-037.a, DD-004).

Ingests the same 10k-span set in generation order, reversed, and shuffled into random batch
sizes, each into a fresh database, and compares row-level hashes of ``runs`` and ``sessions``
(natural keys only: surrogate pks are replaced by trace/span IDs, service names, external IDs).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sqlalchemy import text  # noqa: E402

import gen  # noqa: E402
import writer  # noqa: E402

NOW_NS = gen.BASE_NS + 40 * 86_400 * gen.NS  # fixed clock: after the dataset

RUNS_SQL = """
SELECT r.trace_id, src.service_name, r.source_count, ses.external_id, r.status, r.start_ns,
       r.end_ns, r.duration_ns, r.last_span_ns, root.span_id, r.root_name, r.agent_name,
       r.is_genai, r.models, r.input_tokens, r.output_tokens, r.cached_input_tokens,
       r.cache_write_tokens, r.reasoning_tokens, r.usage_source, r.cost_nusd, r.unpriced_calls,
       r.llm_calls, r.tool_calls, r.error_spans, r.span_count, r.missing_parents, r.bytes_est
FROM runs r LEFT JOIN sources src ON src.pk = r.source_pk
LEFT JOIN sessions ses ON ses.pk = r.session_pk LEFT JOIN spans root ON root.pk = r.root_span_pk
ORDER BY r.trace_id
"""
SESSIONS_SQL = """
SELECT external_id, first_ns, last_ns, run_count, input_tokens, output_tokens,
       cached_input_tokens, cost_nusd, includes_unpriced FROM sessions ORDER BY external_id
"""


def norm(v):
    if isinstance(v, bytes | memoryview):
        return bytes(v).hex()
    if isinstance(v, str) and v.startswith("["):
        return json.loads(v)  # SQLite JSON text vs. PG decoded list
    if isinstance(v, bool):
        return int(v)
    return v


async def table_hash(conn, sql: str) -> tuple[str, int]:
    h = hashlib.blake2b(digest_size=16)
    rows = (await conn.execute(text(sql))).all()
    for row in rows:
        h.update(json.dumps([norm(v) for v in row], sort_keys=True).encode())
    return h.hexdigest(), len(rows)


def batches(spans: list, sizes) -> list[list]:
    out, i = [], 0
    for n in sizes:
        if i >= len(spans):
            break
        out.append(spans[i : i + n])
        i += n
    return out


async def ingest(backend: str, name: str, workdir: Path, groups: list[list]) -> dict:
    engine = await writer.fresh(backend, name, workdir)
    caches = writer.Caches()
    for g in groups:
        async with engine.begin() as conn:
            await writer.write_group(conn, g, NOW_NS, caches)
    async with engine.connect() as conn:
        runs = await table_hash(conn, RUNS_SQL)
        sessions = await table_hash(conn, SESSIONS_SQL)
    await engine.dispose()
    return {"runs": runs, "sessions": sessions, "groups": len(groups)}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--backends", default="sqlite,postgresql")
    ap.add_argument("--spans", type=int, default=10_000)
    args = ap.parse_args()
    runs = gen.dataset(seed=7, spans_target=args.spans)
    spans = [s for r in runs for s in r]
    rng = random.Random(99)
    shuffled = spans[:]
    rng.shuffle(shuffled)
    orders = {
        "generation": batches(spans, [512] * 100),
        "reversed": batches(spans[::-1], [512] * 100),
        "shuffled": batches(shuffled, [rng.randint(1, 1500) for _ in range(10_000)]),
    }
    # duplicate delivery on top of shuffled: re-send 10 % of the spans (idempotency, REQ-038)
    orders["shuffled+dups"] = orders["shuffled"] + batches(
        rng.sample(spans, len(spans) // 10), [700] * 50
    )
    report = {"spans": len(spans), "runs": len(runs)}
    ok = True
    for backend in args.backends.split(","):
        res = {}
        for label, groups in orders.items():
            res[label] = await ingest(
                backend, f"s2_order_{label.replace('+', '_')}", args.workdir, groups
            )
        hashes = {(v["runs"], v["sessions"]) for v in res.values()}
        report[backend] = {"identical": len(hashes) == 1, **res}
        ok &= len(hashes) == 1
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
