"""Tests for scripts/check_upgrade_head.py (plan task P2-11.1, REQ-074)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_upgrade_head", ROOT / "scripts/check_upgrade_head.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sqlite_upgrade_head_from_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert _load().main(["sqlite"]) == 0
    assert "OK: sqlite" in capsys.readouterr().out


def test_pg_requires_a_url() -> None:
    with pytest.raises(SystemExit):
        _load().main(["pg"])


@pytest.mark.pg
def test_pg_upgrade_head_from_empty(pg_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _load().main(["pg", "--url", pg_url]) == 0
    assert "OK: pg" in capsys.readouterr().out
