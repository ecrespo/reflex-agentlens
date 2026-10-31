"""Smoke tests for the package scaffold (plan task P0-01.1, Tech Design DD-022)."""

import subprocess
import sys

import reflex_agentlens
from reflex_agentlens.cli.main import main


def test_package_exposes_version() -> None:
    assert reflex_agentlens.__version__ == "0.1.0.dev0"


def test_cli_version_prints_package_version(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main([]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0.dev0"


def test_cli_entry_point_runs_as_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "reflex_agentlens.cli.main"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "0.1.0.dev0"
