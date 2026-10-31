# /// script
# requires-python = ">=3.11"
# dependencies = ["sqlalchemy[asyncio]>=2.0.36", "aiosqlite>=0.22.1", "asyncpg>=0.32.0"]
# ///
"""Spike S2b: write-path throughput with group commit (Task P0-08.1, REQ-300.a, DD-003).

4 producers submit 512-span batches to a bounded queue; one writer drains it into groups of
<= 50 ms or <= 5,000 spans, each one transaction doing Q6 + Q7. Producers replay a pre-generated
pool and rewrite the first 4 bytes of each trace ID per cycle, so every span is new and generation
cost stays out of the measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gen  # noqa: E402
import writer  # noqa: E402

PRODUCERS = 4
BATCH = 512
GROUP_MAX_SPANS = 5_000
GROUP_MAX_S = 0.050


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[max(0, math.ceil(len(xs) * p) - 1)] if xs else 0.0


async def producer(k: int, pool: list, q: asyncio.Queue, stop: asyncio.Event) -> None:
    cycle = 0
    while not stop.is_set():
        cycle += 1
        prefix = (k * 1_000_000 + cycle).to_bytes(4, "big")
        for i in range(0, len(pool), BATCH):
            if stop.is_set():
                return
            batch = [
                dataclasses.replace(s, trace_id=prefix + s.trace_id[4:])
                for s in pool[i : i + BATCH]
            ]
            await q.put(batch)


async def run(backend: str, workdir: Path, seconds: float) -> dict:
    pool = [s for r in gen.dataset(seed=11, spans_target=50_000) for s in r]
    engine = await writer.fresh(backend, "s2_write", workdir)
    caches = writer.Caches()
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    stop = asyncio.Event()
    tasks = [asyncio.create_task(producer(k, pool, q, stop)) for k in range(PRODUCERS)]
    now = gen.BASE_NS + 40 * 86_400 * gen.NS
    commits, sizes, inserted = [], [], 0
    t_end = time.perf_counter() + seconds
    t0 = time.perf_counter()
    while time.perf_counter() < t_end:
        group = await q.get()
        deadline = time.perf_counter() + GROUP_MAX_S
        while len(group) < GROUP_MAX_SPANS:
            left = deadline - time.perf_counter()
            if left <= 0:
                break
            try:
                group += await asyncio.wait_for(q.get(), left)
            except TimeoutError:
                break
        c0 = time.perf_counter()
        async with engine.begin() as conn:
            res = await writer.write_group(conn, group, now, caches)
        commits.append(time.perf_counter() - c0)
        sizes.append(len(group))
        inserted += res["inserted"]
    elapsed = time.perf_counter() - t0
    stop.set()
    while not q.empty():
        q.get_nowait()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine.dispose()
    return {
        "seconds": round(elapsed, 1),
        "spans_inserted": inserted,
        "spans_per_s": round(inserted / elapsed),
        "groups": len(sizes),
        "group_size_mean": round(statistics.mean(sizes)),
        "group_size_p50": pct(sizes, 0.5),
        "commit_ms_p50": round(pct(commits, 0.5) * 1000, 1),
        "commit_ms_p95": round(pct(commits, 0.95) * 1000, 1),
        "commit_ms_max": round(max(commits) * 1000, 1),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--backends", default="sqlite,postgresql")
    args = ap.parse_args()
    report = {}
    for backend in args.backends.split(","):
        report[backend] = await run(backend, args.workdir, args.seconds)
        print(json.dumps({backend: report[backend]}), file=sys.stderr, flush=True)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
