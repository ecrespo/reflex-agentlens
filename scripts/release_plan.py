"""Version facts for the release workflow (plan task P7-05.1; REQ-206, Tech Design §9.1).

    uv run --no-project python scripts/release_plan.py version              # 0.1.0a1
    uv run --no-project python scripts/release_plan.py prerelease 0.1.0a1   # true
    uv run --no-project python scripts/release_plan.py image-tags 0.1.0a1   # 0.1.0-alpha.1

The version is read from ``src/reflex_agentlens/__init__.py`` without importing the package.
Image tags: a final ``X.Y.Z`` gets ``X.Y.Z X.Y latest``; a pre-release only its own tag in
SemVer form (``0.1.0a1`` → ``0.1.0-alpha.1``, ``b`` → ``beta``, ``rc`` → ``rc``,
``.devN`` → ``dev.N``).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

INIT = Path(__file__).resolve().parents[1] / "src" / "reflex_agentlens" / "__init__.py"
_VERSION = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)(?:(?P<pre>a|b|rc)(?P<pre_n>\d+))?(?:\.dev(?P<dev>\d+))?$"
)
_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}


def read_version(init: Path = INIT) -> str:
    match = re.search(r'^__version__ = "([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise ValueError(f"no __version__ in {init}")
    return match.group(1)


def _parse(version: str) -> re.Match[str]:
    match = _VERSION.match(version)
    if match is None:
        raise ValueError(f"unsupported version {version!r} (X.Y.Z, X.Y.ZaN/bN/rcN, .devN)")
    return match


def is_prerelease(version: str) -> bool:
    match = _parse(version)
    return match["pre"] is not None or match["dev"] is not None


def image_tags(version: str) -> list[str]:
    match = _parse(version)
    release = match["release"]
    if not is_prerelease(version):
        major, minor, _ = release.split(".")
        return [release, f"{major}.{minor}", "latest"]
    parts = []
    if match["pre"] is not None:
        parts.append(f"{_PRE[match['pre']]}.{match['pre_n']}")
    if match["dev"] is not None:
        parts.append(f"dev.{match['dev']}")
    return [f"{release}-{'.'.join(parts)}"]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["version"]:
        out = read_version()
    elif len(args) == 2 and args[0] == "prerelease":
        out = "true" if is_prerelease(args[1]) else "false"
    elif len(args) == 2 and args[0] == "image-tags":
        out = " ".join(image_tags(args[1]))
    else:
        sys.stderr.write("usage: release_plan.py version | prerelease V | image-tags V\n")
        return 2
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
