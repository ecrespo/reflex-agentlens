"""Changelog section from Conventional Commits (plan task P7-05.1; REQ-206, Constitution Art. 10).

Usage:
    uv run python scripts/changelog.py 0.1.0a1                   # print the section
    uv run python scripts/changelog.py 0.1.0a1 --since v0.0.9    # commits after that tag
    uv run python scripts/changelog.py 0.1.0a1 --write CHANGELOG.md

Without ``--since`` the range starts at the latest ``v*`` tag reachable from ``HEAD`` (or at the
first commit). ``--write`` inserts the section after the ``## [Unreleased]`` block (or before
the first version section) and leaves the rest of the file untouched; running it twice for the
same version is an error.

Commit types map to Keep a Changelog headings; merge commits and types that do not change the
shipped package (``docs``, ``test``, ``ci``, ``chore``, ``style``, ``spike``) are left out.
Breaking changes (``type!:`` or a ``BREAKING CHANGE:`` footer) get their own heading.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

HEADINGS = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Performance",
    "refactor": "Changed",
    "build": "Packaging",
    "revert": "Reverted",
}
ORDER = ["Breaking changes", "Added", "Changed", "Fixed", "Performance", "Packaging", "Reverted"]
_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<text>.+)$")
_TASK = re.compile(r"\s*\((?:P\d+-\d+(?:\.\d+)?(?:,\s*)?)+\)\s*$")  # trailing "(P7-01.1)"
_SEP_FIELD, _SEP_RECORD = "\x1f", "\x1e"
REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Commit:
    type: str
    scope: str | None
    text: str
    breaking: bool


def parse(subject: str, body: str = "") -> Commit | None:
    """A Conventional Commit, or ``None`` for merges and non-conforming subjects."""
    match = _SUBJECT.match(subject.strip())
    if match is None:
        return None
    breaking = bool(match["bang"]) or re.search(r"^BREAKING[ -]CHANGE: ", body, re.M) is not None
    text = _TASK.sub("", match["text"]).strip()
    return Commit(match["type"], match["scope"], text, breaking)


def heading(commit: Commit) -> str | None:
    if commit.breaking:
        return "Breaking changes"
    return HEADINGS.get(commit.type)


def render(version: str, date: dt.date, commits: Iterable[Commit]) -> str:
    """The Markdown section for ``version``."""
    groups: dict[str, list[str]] = {}
    for commit in commits:
        name = heading(commit)
        if name is None:
            continue
        scope = f"**{commit.scope}:** " if commit.scope else ""
        line = f"- {scope}{commit.text}"
        if line not in groups.setdefault(name, []):
            groups[name].append(line)
    out = [f"## [{version}] - {date.isoformat()}", ""]
    if not groups:
        out += ["No user-facing changes.", ""]
    for name in ORDER:
        if name in groups:
            out += [f"### {name}", "", *groups[name], ""]
    return "\n".join(out)


def insert(changelog: str, section: str, version: str) -> str:
    """``changelog`` with ``section`` placed after ``## [Unreleased]`` (or the title)."""
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.M):
        raise ValueError(f"CHANGELOG already has a section for {version}")
    lines = changelog.splitlines(keepends=True)
    anchor = next((i for i, line in enumerate(lines) if line.startswith("## [Unreleased]")), None)
    if anchor is not None:
        # after the Unreleased block: before the next "## " heading, or at the end
        end = next((i for i in range(anchor + 1, len(lines)) if lines[i].startswith("## ")), None)
        at = len(lines) if end is None else end
    else:  # before the first version section, after the title and its intro
        at = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    head = "".join(lines[:at]).rstrip("\n") + "\n\n"
    tail = "".join(lines[at:]).lstrip("\n")
    return head + section.rstrip("\n") + "\n" + ("\n" + tail if tail else "")


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv
        ["git", *args],  # noqa: S607 - git from PATH, as in every developer and CI shell
        check=True, capture_output=True, text=True, cwd=REPO,
    ).stdout  # fmt: skip


def commits_since(since: str | None) -> list[Commit]:
    if since is None:
        found = _git("describe", "--tags", "--abbrev=0", "--match", "v*", "HEAD").strip() if (
            _git("tag", "--list", "v*").strip()
        ) else ""  # fmt: skip
        since = found or None
    span = "HEAD" if since is None else f"{since}..HEAD"
    raw = _git("log", "--no-merges", f"--format=%s{_SEP_FIELD}%b{_SEP_RECORD}", span)
    out = []
    for record in raw.split(_SEP_RECORD):
        if not record.strip():
            continue
        subject, _, body = record.strip("\n").partition(_SEP_FIELD)
        commit = parse(subject, body)
        if commit is not None:
            out.append(commit)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/changelog.py", description=__doc__.split("\n")[0]
    )
    parser.add_argument("version", help="the version being released, e.g. 0.1.0a1")
    parser.add_argument("--since", help="start after this ref (default: the latest v* tag)")
    parser.add_argument("--date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--write", type=Path, help="insert the section into this changelog")
    args = parser.parse_args(argv)
    date = args.date or dt.datetime.now(dt.UTC).date()
    section = render(args.version, date, commits_since(args.since))
    if args.write is None:
        sys.stdout.write(section)
        return 0
    try:
        args.write.write_text(insert(args.write.read_text(), section, args.version))
    except ValueError as exc:
        sys.stderr.write(f"changelog: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
