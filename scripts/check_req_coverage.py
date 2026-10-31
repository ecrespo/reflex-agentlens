"""List MUST acceptance criteria of the PRD that no test cites (REQ-362.a, Constitution Art. 8).

Usage: uv run python scripts/check_req_coverage.py [--prd PATH] [--tests DIR]
                                                   [--exemptions PATH] [--report-only]

Rules:
- A requirement is a ``#### REQ-NNN — Title · PRIORITY`` heading. Only ``MUST`` requirements count.
- A criterion is a bullet ``- **REQ-NNN.x** …`` under its requirement, including indented
  continuation lines.
- A criterion whose text ends with a bare ``SHOULD.`` or ``COULD.`` is excluded. A marker inside
  the sentence or in parentheses (``(SHOULD: p99)``, ``… (SHOULD).``) only weakens part of the
  criterion, so the criterion still counts.
- A criterion is cited when its exact ID (``REQ-NNN.x``) appears in any file under the tests
  directory (test names, docstrings or comments), except under ``tests/fixtures/`` (data).
- Exemptions live in ``tests/req_exemptions.toml`` as ``"REQ-NNN.x" = "reason"`` under
  ``[exemptions]``. An empty reason is an error.

Exit code: 1 if any MUST criterion is uncited and not exempt (0 with ``--report-only``),
2 on invalid input.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENT_RE = re.compile(r"^#{2,6}\s+(REQ-\d{3})\b.*·\s*(MUST|SHOULD|COULD|WON'T|WONT)\s*$")
CRITERION_RE = re.compile(r"^\s*-\s+\*\*(REQ-\d{3}\.[a-z]+)\*\*\s*(.*)$")
HEADING_RE = re.compile(r"^#{1,6}\s")
WEAK_TAIL_RE = re.compile(r"(?<!\()\b(SHOULD|COULD)\.\s*$")
CITATION_RE = re.compile(r"\bREQ-\d{3}\.[a-z]+\b")
TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".json", ".yaml", ".yml", ".feature"}


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    requirement: str
    priority: str
    text: str

    @property
    def is_must(self) -> bool:
        return self.priority == "MUST" and not WEAK_TAIL_RE.search(self.text)


def parse_prd(text: str) -> list[Criterion]:
    """Return every criterion of the PRD with the priority of its requirement."""
    criteria: list[Criterion] = []
    requirement: str | None = None
    priority = ""
    current: list[str] | None = None
    current_id = ""

    def flush() -> None:
        nonlocal current
        if current is not None and requirement is not None:
            criteria.append(Criterion(current_id, requirement, priority, " ".join(current).strip()))
        current = None

    for line in text.splitlines():
        if m := REQUIREMENT_RE.match(line):
            flush()
            requirement, priority = m.group(1), m.group(2)
            continue
        if HEADING_RE.match(line):
            flush()
            requirement = None
            continue
        if requirement is None:
            continue
        if m := CRITERION_RE.match(line):
            flush()
            current_id, current = m.group(1), [m.group(2)]
            continue
        if current is not None and line.startswith((" ", "\t")) and line.strip():
            current.append(line.strip())
            continue
        flush()
    flush()
    return criteria


def find_citations(tests_dir: Path) -> set[str]:
    cited: set[str] = set()
    for path in tests_dir.rglob("*"):
        rel = path.relative_to(tests_dir).parts
        if rel[0] == "fixtures" or path.name == "req_exemptions.toml":
            continue
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            cited.update(CITATION_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    return cited


def load_exemptions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = data.get("exemptions", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: [exemptions] must be a table")
    exemptions: dict[str, str] = {}
    for key, reason in raw.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{path}: exemption {key} needs a non-empty reason")
        exemptions[key] = reason.strip()
    return exemptions


@dataclass(frozen=True, slots=True)
class Report:
    must: list[Criterion]
    uncited: list[Criterion]
    exempt: list[Criterion]
    unknown_exemptions: list[str]


def check(prd: str, tests_dir: Path, exemptions: dict[str, str]) -> Report:
    must = [c for c in parse_prd(prd) if c.is_must]
    cited = find_citations(tests_dir)
    must_ids = {c.id for c in must}
    exempt = [c for c in must if c.id in exemptions and c.id not in cited]
    uncited = [c for c in must if c.id not in cited and c.id not in exemptions]
    unknown = sorted(set(exemptions) - must_ids)
    return Report(must, uncited, exempt, unknown)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--prd", type=Path, default=ROOT / "specs/prd/agentlens-v0.1.md")
    ap.add_argument("--tests", type=Path, default=ROOT / "tests")
    ap.add_argument("--exemptions", type=Path, default=ROOT / "tests/req_exemptions.toml")
    ap.add_argument("--report-only", action="store_true", help="always exit 0")
    args = ap.parse_args(argv)
    try:
        exemptions = load_exemptions(args.exemptions)
        report = check(args.prd.read_text(encoding="utf-8"), args.tests, exemptions)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for c in report.uncited:
        print(f"UNCITED  {c.id}  {c.text[:100]}")
    for c in report.exempt:
        print(f"EXEMPT   {c.id}  {exemptions[c.id]}")
    for key in report.unknown_exemptions:
        print(f"WARNING  exemption {key} is not a MUST criterion of the PRD", file=sys.stderr)
    covered = len(report.must) - len(report.uncited) - len(report.exempt)
    print(
        f"\n{len(report.must)} MUST criteria: {covered} cited, {len(report.exempt)} exempt, "
        f"{len(report.uncited)} uncited"
    )
    if report.uncited and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
