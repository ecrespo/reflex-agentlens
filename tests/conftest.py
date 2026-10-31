"""Shared pytest configuration (plan task P1-02.3, REQ-361.a).

- Hypothesis profiles: ``ci`` (more examples, derandomized, no deadline) and ``dev`` (fast).
  Selected with ``HYPOTHESIS_PROFILE``; defaults to ``dev``.
- Tests marked ``pg`` are skipped unless ``AGENTLENS_TEST_PG_URL`` is set.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import settings

settings.register_profile("ci", max_examples=300, derandomize=True, deadline=None)
settings.register_profile("dev", max_examples=50, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

PG_URL_ENV = "AGENTLENS_TEST_PG_URL"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(PG_URL_ENV):
        return
    skip_pg = pytest.mark.skip(reason=f"{PG_URL_ENV} is not set")
    for item in items:
        if "pg" in item.keywords:
            item.add_marker(skip_pg)


@pytest.fixture
def pg_url() -> str:
    """PostgreSQL URL for ``pg`` tests (only reached when the variable is set)."""
    return os.environ[PG_URL_ENV]
