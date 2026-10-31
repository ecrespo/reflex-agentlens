"""The package tree of Tech Design §5.1 imports cleanly (plan task P1-01.1, DD-022)."""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "core",
    "core.semconv",
    "ingest",
    "ingest.normalize",
    "ingest.redaction",
    "store",
    "store.sqlite",
    "store.postgres",
    "store.migrations",
    "query",
    "components",
    "pages",
    "app",
    "cli",
    "data",
    "_webapp",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_subpackage_imports_and_is_documented(name: str) -> None:
    module = importlib.import_module(f"reflex_agentlens.{name}")
    assert module.__doc__, f"reflex_agentlens.{name} needs a one-line docstring"
