"""Spike S1 browser smoke check (Tasks P0-03.1, P0-03.2; first pass of P0-05.2).

Usage: python smoke.py URL
Checks: chart, Mermaid graph and DTO rows render; a click round-trips over the websocket;
records console errors and every request to a non-app origin.
"""

from __future__ import annotations

import json
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main(url: str) -> int:
    origin = urlparse(url).netloc
    foreign: list[str] = []
    console_errors: list[str] = []
    result: dict[str, object] = {"url": url}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on(
            "request",
            lambda r: foreign.append(r.url)
            if urlparse(r.url).netloc not in (origin, "") and not r.url.startswith("data:")
            else None,
        )
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        t0 = time.monotonic()
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector(".run-row", timeout=15000)
        result["first_render_s"] = round(time.monotonic() - t0, 2)
        result["run_rows"] = page.locator(".run-row").count()
        result["svg_count"] = page.locator("svg").count()
        page.wait_for_selector("#graph svg", timeout=15000)
        result["mermaid_svg"] = page.locator("#graph svg").count() > 0
        lat = []
        for i in range(1, 21):
            t = time.monotonic()
            page.click("#bump")
            page.wait_for_function(
                f"document.querySelector('#clicks').innerText.includes('{i}')", timeout=5000
            )
            lat.append(time.monotonic() - t)
        lat.sort()
        result["event_roundtrip_p50_ms"] = round(lat[len(lat) // 2] * 1000, 1)
        result["event_roundtrip_max_ms"] = round(lat[-1] * 1000, 1)
        result["run_rows_after"] = page.locator(".run-row").count()
        browser.close()
    result["foreign_requests"] = foreign
    result["console_errors"] = console_errors
    print(json.dumps(result, indent=2))
    ok = (
        result["run_rows"] == 2
        and result["run_rows_after"] == 22
        and result["mermaid_svg"]
        and not foreign
        and not console_errors
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
