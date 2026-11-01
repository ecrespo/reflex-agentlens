"""Browser smoke suite (plan task P6-12.1; REQ-190.b/d, 193.a, 324, 327, 363).

Runs in the CI ``smoke`` job (``AGENTLENS_SMOKE=1``, frontend built, Chromium installed).
Playwright's ``wait_for_function`` evaluates strings and is blocked by the CSP (S1 finding), so
the tests wait with locator assertions.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, expect

from tests.smoke.conftest import EVIL_HOST, XSS_SPAN, XSS_TRACE, Server, watched

pytestmark = pytest.mark.smoke

PAGES = [
    ("/", "Dashboard"),
    ("/runs", "Runs"),
    ("/sessions", "Sessions (threads)"),
    ("/settings", "Settings"),
    ("/settings?tab=redaction", "Settings"),
]


@pytest.mark.parametrize(("path", "title"), PAGES)
def test_standalone_pages_render_cleanly_req_324_363(
    browser: Browser, standalone: Server, path: str, title: str
) -> None:
    page, watch = watched(browser, standalone.base)
    try:
        response = page.goto(standalone.base + path)
        assert response is not None
        assert response.ok
        expect(page.locator("h1.agentlens-title")).to_have_text(title)
        expect(page.locator("header.agentlens-shell")).to_be_visible()
        page.wait_for_timeout(500)  # late errors and requests of the first render
        watch.assert_clean()
    finally:
        page.close()


def test_runs_lists_the_seeded_run(browser: Browser, standalone: Server) -> None:
    page, watch = watched(browser, standalone.base)
    try:
        page.goto(standalone.base + "/runs")
        expect(page.get_by_text("shop").first).to_be_visible()
        watch.assert_clean()
    finally:
        page.close()


def test_csp_header_is_enforced_req_193a(browser: Browser, standalone: Server) -> None:
    page, watch = watched(browser, standalone.base)
    try:
        response = page.goto(standalone.base + "/")
        assert response is not None
        policy = response.headers["content-security-policy"]
        assert "'sha256-" in policy
        assert "unsafe-eval" not in policy
        assert "https:" not in policy  # no third-party origin
        expect(page.locator("h1.agentlens-title")).to_have_text("Dashboard")
        watch.assert_clean()  # the probe logs every securitypolicyviolation as an error
    finally:
        page.close()


def test_hostile_content_never_executes_req_327(browser: Browser, standalone: Server) -> None:
    trace = XSS_TRACE.to_bytes(16, "big").hex()
    span = XSS_SPAN.to_bytes(8, "big").hex()
    page, watch = watched(browser, standalone.base)
    try:
        page.goto(f"{standalone.base}/trace/{trace}?span={span}")
        expect(page.get_by_text("Messages")).to_be_visible()
        expect(page.get_by_text("Assistant")).to_be_visible()
        page.wait_for_timeout(1000)
        assert page.evaluate("window.__xss === undefined")
        hrefs = page.locator("a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
        assert not any(h and h.strip().lower().startswith("javascript:") for h in hrefs)
        assert page.locator("iframe").count() == 0
        assert page.locator("img[src*='evil']").count() == 0
        assert not any(EVIL_HOST in url for url in watch.outbound)
        watch.assert_clean()
    finally:
        page.close()


def test_host_app_keeps_its_states_and_routes_req_190b_d(browser: Browser, host: Server) -> None:
    page, watch = watched(browser, host.base)
    try:
        page.goto(host.base + "/runs")
        expect(page.locator("#host-title")).to_have_text("host runs")
        page.click("#host-click")
        page.click("#host-click")
        expect(page.locator("#host-clicks")).to_have_text("2")
        page.goto(host.base + "/settings")
        expect(page.locator("#host-title")).to_have_text("host settings")
        page.goto(host.base + "/agentlens/runs")
        expect(page.locator("h1.agentlens-title")).to_have_text("Runs")
        page.goto(host.base + "/agentlens/settings")
        expect(page.locator("h1.agentlens-title")).to_have_text("Settings")
        page.goto(host.base + "/runs")  # the host's own page still works after ours
        expect(page.locator("#host-title")).to_have_text("host runs")
        watch.assert_clean()
    finally:
        page.close()
