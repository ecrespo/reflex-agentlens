"""Checks that the test configuration itself is wired (plan task P1-02.3, REQ-361.a)."""

from __future__ import annotations

import asyncio

import pytest


async def test_async_tests_run_without_marker() -> None:
    """asyncio_mode = "auto": a bare ``async def`` test runs on an event loop."""
    await asyncio.sleep(0)
    assert asyncio.get_running_loop().is_running()


@pytest.mark.pg
def test_pg_marker_is_skipped_without_url(pg_url: str) -> None:
    """Runs only when AGENTLENS_TEST_PG_URL is set; otherwise collected and skipped."""
    assert pg_url.startswith("postgresql")
