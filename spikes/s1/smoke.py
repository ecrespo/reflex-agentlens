"""Spike S1 browser smoke check (Tasks P0-03.1, P0-03.2; first pass of P0-05.2).

Usage: python smoke.py URL
Checks: chart, Mermaid graph and DTO rows render; a click round-trips over the websocket;
records console errors and every request to a non-app origin.
"""

from __future__ import annotations

import json
import re
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


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
            lambda r: (
                foreign.append(r.url)
                if urlparse(r.url).netloc not in (origin, "") and not r.url.startswith("data:")
                else None
            ),
        )
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.add_init_script(
            "window.__csp = []; document.addEventListener('securitypolicyviolation', "
            "e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));"
        )
        t0 = time.monotonic()
        resp = page.goto(url, wait_until="networkidle")
        result["csp_header"] = resp.headers.get("content-security-policy", "")[:80]
        page.wait_for_selector(".run-row", timeout=15000)
        result["first_render_s"] = round(time.monotonic() - t0, 2)
        result["run_rows"] = page.locator(".run-row").count()
        result["svg_count"] = page.locator("svg").count()
        page.wait_for_selector("#graph svg", timeout=15000)
        result["mermaid_svg"] = page.locator("#graph svg").count() > 0
        box = page.locator("svg").first.bounding_box()  # sweep the chart (tooltips/hover state)
        result["chart_box"] = bool(box)
        if box:
            for k in range(1, 20):
                page.mouse.move(box["x"] + box["width"] * k / 20, box["y"] + box["height"] / 2)
                page.wait_for_timeout(30)
        lat = []
        for i in range(1, 21):
            t = time.monotonic()
            page.click("#bump")
            expect(page.locator("#clicks")).to_have_text(re.compile(rf"\b{i}$"), timeout=5000)
            lat.append(time.monotonic() - t)
        lat.sort()
        result["event_roundtrip_p50_ms"] = round(lat[len(lat) // 2] * 1000, 1)
        result["event_roundtrip_max_ms"] = round(lat[-1] * 1000, 1)
        result["run_rows_after"] = page.locator(".run-row").count()
        result["csp_violations"] = page.evaluate("window.__csp")
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
        and not result["csp_violations"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
