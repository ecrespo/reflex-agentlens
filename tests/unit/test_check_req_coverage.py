"""Tests for scripts/check_req_coverage.py (plan task P1-05.1, REQ-362.a)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/req_coverage/prd.md"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_req_coverage", ROOT / "scripts/check_req_coverage.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


crc = _load()


def _write_tests(tmp_path: Path, body: str) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(body)
    return tests


def test_req_362a_parser_selects_only_must_criteria() -> None:
    criteria = crc.parse_prd(FIXTURE.read_text())
    assert [c.id for c in criteria] == [
        "REQ-901.a",
        "REQ-901.b",
        "REQ-901.c",
        "REQ-901.d",
        "REQ-901.e",
        "REQ-902.a",
        "REQ-903.a",
        "REQ-903.b",
    ]
    must = [c.id for c in criteria if c.is_must]
    # .b and .e end with a bare SHOULD./COULD.; .c and .d only weaken part of the criterion;
    # REQ-902 is a SHOULD requirement.
    assert must == ["REQ-901.a", "REQ-901.c", "REQ-901.d", "REQ-903.a", "REQ-903.b"]


def test_req_362a_continuation_lines_belong_to_the_criterion() -> None:
    by_id = {c.id: c for c in crc.parse_prd(FIXTURE.read_text())}
    assert by_id["REQ-901.b"].text.endswith("across a continuation line. SHOULD.")


def test_req_362a_uncited_criteria_fail_unless_report_only(tmp_path: Path) -> None:
    tests = _write_tests(tmp_path, '"""Covers REQ-901.a and REQ-903.a."""\n')
    args = ["--prd", str(FIXTURE), "--tests", str(tests), "--exemptions", str(tmp_path / "none")]
    assert crc.main(args) == 1
    assert crc.main([*args, "--report-only"]) == 0


def test_req_362a_exemptions_and_citations_cover_everything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tests = _write_tests(tmp_path, "# REQ-901.a REQ-901.c REQ-901.d REQ-903.a\n")
    exemptions = tmp_path / "ex.toml"
    exemptions.write_text('[exemptions]\n"REQ-903.b" = "aspirational metric"\n"REQ-999.z" = "x"\n')
    rc = crc.main(["--prd", str(FIXTURE), "--tests", str(tests), "--exemptions", str(exemptions)])
    out = capsys.readouterr()
    assert rc == 0
    assert "EXEMPT   REQ-903.b  aspirational metric" in out.out
    assert "exemption REQ-999.z is not a MUST criterion" in out.err
    assert "5 MUST criteria: 4 cited, 1 exempt, 0 uncited" in out.out


def test_req_362a_citations_ignore_fixtures(tmp_path: Path) -> None:
    tests = _write_tests(tmp_path, "")
    (tests / "fixtures").mkdir()
    (tests / "fixtures" / "data.json").write_text('{"note": "REQ-901.a"}')
    assert crc.find_citations(tests) == set()


def test_req_362a_empty_exemption_reason_is_rejected(tmp_path: Path) -> None:
    exemptions = tmp_path / "ex.toml"
    exemptions.write_text('[exemptions]\n"REQ-901.a" = " "\n')
    tests = _write_tests(tmp_path, "")
    args = ["--prd", str(FIXTURE), "--tests", str(tests), "--exemptions", str(exemptions)]
    assert crc.main(args) == 2
