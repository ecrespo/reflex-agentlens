"""Changelog generation from Conventional Commits (plan task P7-05.1; REQ-206, Art. 10)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
DAY = dt.date(2026, 11, 2)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "changelog_under_test", ROOT / "scripts" / "changelog.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


cl = _module()


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("feat(docker): compose.yaml try-it topology (P7-03.1)", "",
         ("feat", "docker", "compose.yaml try-it topology", False)),
        ("fix: blank is missing", "", ("fix", None, "blank is missing", False)),
        ("feat(api)!: rename mount()", "", ("feat", "api", "rename mount()", True)),
        ("refactor: x", "text\n\nBREAKING CHANGE: y", ("refactor", None, "x", True)),
        ("docs(tasks): batch 2 (P7-02.1, P7-02.2)", "", ("docs", "tasks", "batch 2", False)),
    ],
)  # fmt: skip
def test_parse_conventional_commits(
    subject: str, body: str, expected: tuple[str, str | None, str, bool]
) -> None:
    commit = cl.parse(subject, body)
    assert commit is not None
    assert (commit.type, commit.scope, commit.text, commit.breaking) == expected


@pytest.mark.parametrize("subject", ["Merge pull request #44 from x/y", "WIP stuff", "Feat: no"])
def test_parse_skips_non_conforming_subjects(subject: str) -> None:
    assert cl.parse(subject) is None


def test_render_groups_orders_and_drops_internal_types() -> None:
    commits = [
        cl.parse(s)
        for s in (
            "fix(config): blank password is missing",
            "feat(docker): image",
            "docs: readme",
            "test(docker): checks",
            "feat(docker): image",  # duplicate line, listed once
            "build: wheel hook",
            "feat!: new config format",
        )
    ]
    text = cl.render("0.1.0a1", DAY, commits)
    assert text.splitlines()[0] == "## [0.1.0a1] - 2026-11-02"
    assert text.index("### Breaking changes") < text.index("### Added") < text.index("### Fixed")
    assert text.index("### Fixed") < text.index("### Packaging")
    assert text.count("- **docker:** image") == 1
    assert "readme" not in text
    assert "checks" not in text


def test_render_without_user_facing_changes() -> None:
    assert "No user-facing changes." in cl.render("0.1.1", DAY, [cl.parse("docs: x")])


CHANGELOG = """# Changelog

Intro.

## [Unreleased]

### Added

- Something not released yet.

## [0.0.1] - 2026-10-01

- old
"""


def test_insert_after_the_unreleased_block() -> None:
    section = cl.render("0.1.0a1", DAY, [cl.parse("feat: new")])
    out = cl.insert(CHANGELOG, section, "0.1.0a1")
    assert out.index("## [Unreleased]") < out.index("## [0.1.0a1]") < out.index("## [0.0.1]")
    assert "- Something not released yet." in out
    assert "\n\n\n" not in out
    with pytest.raises(ValueError, match="already has a section"):
        cl.insert(out, section, "0.1.0a1")


def test_insert_below_the_title_without_unreleased() -> None:
    out = cl.insert("# Changelog\n\nIntro.\n", "## [1.0.0] - 2026-11-02\n\n- x\n", "1.0.0")
    assert out == "# Changelog\n\nIntro.\n\n## [1.0.0] - 2026-11-02\n\n- x\n"


def test_cli_prints_a_section_from_this_repository(capsys: pytest.CaptureFixture[str]) -> None:
    assert cl.main(["9.9.9", "--date", "2026-11-02"]) == 0
    assert capsys.readouterr().out.startswith("## [9.9.9] - 2026-11-02")
