"""Spike S1c: UI event round-trip while the ingest path is busy (Task P0-04.1, DD-001 GIL concern).

Usage: python ui_latency.py URL [--clicks 200]
Clicks ``#bump`` repeatedly and measures click -> DOM update round trips; prints p50/p95/max.
"""

from __future__ import annotations

import argparse
import json
import re
import math
import time

from playwright.sync_api import expect, sync_playwright


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[max(0, math.ceil(len(xs) * p) - 1)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--clicks", type=int, default=200)
    args = ap.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector(".run-row", timeout=15000)
        lat = []
        for i in range(1, args.clicks + 1):
            t = time.monotonic()
            page.click("#bump")
            expect(page.locator("#clicks")).to_have_text(re.compile(rf"\b{i}$"), timeout=10000)
            lat.append((time.monotonic() - t) * 1000)
        browser.close()
    print(
        json.dumps(
            {
                "clicks": len(lat),
                "p50_ms": round(pct(lat, 0.5), 1),
                "p95_ms": round(pct(lat, 0.95), 1),
                "max_ms": round(max(lat), 1),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
