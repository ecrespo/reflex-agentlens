"""Build module of the standalone frontend (plan task P6-11.1; DD-002, REQ-324).

``uv run python -m reflex_agentlens._webapp.build`` writes the rxconfig of
:func:`rxconfig_source` into a build directory, runs ``reflex export --frontend-only --no-zip``
on :mod:`reflex_agentlens._webapp.app` and copies ``.web/build/client`` to
``reflex_agentlens/_frontend/``, where ``serve`` finds it (P6-11.2). It needs Bun or Node; the
running server never does (REQ-200.b). The wheel build hook (``hatch_build.py``, P7-01.1) loads
this file by path and calls :func:`export` and :func:`write_csp`, so this module imports only the
standard library.

DD-018: :func:`write_csp` stores the inline-script hashes of every exported ``*.html`` in
``csp.json`` next to the bundle; ``serve`` reads them with :func:`read_csp` (and hashes the HTML
itself when the file is missing, as in a dev build).

Every rxconfig agentlens writes, at build time and at runtime, comes from
:func:`rxconfig_source`: telemetry off (Reflex defaults it to on), no sitemap, explicit plugins,
in-memory state (one worker), and CORS pinned when an origin is given.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "APP_MODULE",
    "CSP_FILE",
    "FRONTEND_DIR",
    "bundle_script_hashes",
    "export",
    "frontend_dir",
    "inline_script_hashes",
    "main",
    "read_csp",
    "rxconfig_source",
    "write_csp",
]

APP_MODULE = "reflex_agentlens._webapp.app"
APP_NAME = "agentlens_webapp"
FRONTEND_DIR = "_frontend"
CSP_FILE = "csp.json"
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_PACKAGE = Path(__file__).resolve().parent.parent  # src/reflex_agentlens


def rxconfig_source(*, cors_origin: str | None = None) -> str:
    """The ``rxconfig.py`` text. ``cors_origin`` pins Reflex's CORS (its default is ``*``)."""
    lines = [
        '"""Written by reflex-agentlens; regenerated on every build and start."""',
        "",
        "import reflex as rx",
        "",
        "config = rx.Config(",
        f"    app_name={APP_NAME!r},",
        f"    app_module_import={APP_MODULE!r},",
        "    telemetry_enabled=False,",
        "    show_built_with_reflex=False,",
        '    state_manager_mode="memory",',
        "    plugins=[rx.plugins.RadixThemesPlugin()],",
        "    disable_plugins=[rx.plugins.SitemapPlugin],",
    ]
    if cors_origin is not None:
        lines.append(f"    cors_allowed_origins=[{cors_origin!r}],")
    lines.append(")")
    return "\n".join(lines) + "\n"


def frontend_dir() -> Path:
    """Where the exported frontend lives inside the package."""
    return _PACKAGE / FRONTEND_DIR


def inline_script_hashes(html: str) -> list[str]:
    """CSP source expressions (``'sha256-…'``) for every inline ``<script>`` of ``html``."""
    out = []
    for body in _INLINE_SCRIPT.findall(html):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        out.append(f"'sha256-{digest}'")
    return out


def bundle_script_hashes(client: Path) -> tuple[str, ...]:
    """The sorted, distinct inline-script hashes of every ``*.html`` under ``client``."""
    found: set[str] = set()
    for page in client.rglob("*.html"):
        found.update(inline_script_hashes(page.read_text(encoding="utf-8")))
    return tuple(sorted(found))


def write_csp(client: Path) -> tuple[str, ...]:
    """Write ``csp.json`` (``{"script_hashes": [...]}``) into ``client``; returns the hashes."""
    hashes = bundle_script_hashes(client)
    payload = {"script_hashes": list(hashes)}
    (client / CSP_FILE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return hashes


def read_csp(client: Path) -> tuple[str, ...] | None:
    """The hashes of ``csp.json`` in ``client``, or ``None`` when it is missing or malformed."""
    try:
        payload = json.loads((client / CSP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hashes = payload.get("script_hashes") if isinstance(payload, dict) else None
    if not isinstance(hashes, list) or not all(
        isinstance(h, str) and h.startswith("'sha256-") for h in hashes
    ):
        return None
    return tuple(sorted(set(hashes)))


def export(build_dir: Path, out: Path, *, mark_ignored: bool = True) -> Path:
    """Export the frontend in ``build_dir`` and copy it to ``out``; returns ``out``.
    ``mark_ignored`` drops a ``.gitignore`` into ``out`` (in-tree builds only)."""
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "rxconfig.py").write_text(rxconfig_source())
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_PACKAGE.parent), env.get("PYTHONPATH", "")) if p
    )
    env["REFLEX_TELEMETRY_ENABLED"] = "false"
    reflex = Path(sys.executable).with_name("reflex")
    command = [str(reflex)] if reflex.exists() else [sys.executable, "-m", "reflex"]
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*command, "export", "--frontend-only", "--no-zip", "--loglevel", "warning"],
        cwd=build_dir,
        env=env,
        check=True,
    )
    client = build_dir / ".web" / "build" / "client"
    if not (client / "index.html").is_file():
        raise RuntimeError(f"reflex export produced no {client / 'index.html'}")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(client, out)
    if mark_ignored:
        (out / ".gitignore").write_text("# build output of reflex_agentlens._webapp.build\n*\n")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m reflex_agentlens._webapp.build",
        description="Export the standalone frontend (needs Bun or Node).",
    )
    parser.add_argument("--build-dir", type=Path, default=Path("build/agentlens-webapp"))
    parser.add_argument("--out", type=Path, default=frontend_dir())
    args = parser.parse_args(argv)
    out = export(args.build_dir.resolve(), args.out.resolve())
    write_csp(out)
    files = sum(1 for p in out.rglob("*") if p.is_file())
    sys.stdout.write(f"frontend exported to {out} ({files} files)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
