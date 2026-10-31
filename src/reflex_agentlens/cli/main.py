"""Entry point of the ``reflex-agentlens`` command.

Scaffold stub (plan task P0-01.1): it only prints the package version. The full command set of
API Spec §8.3 is implemented in plan task P5-03.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from reflex_agentlens import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    _ = sys.argv[1:] if argv is None else argv
    print(__version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
