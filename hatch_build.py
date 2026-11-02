"""Hatchling build hook: ship the pre-built frontend inside the wheel (plan task P7-01.1;
Tech Design DD-002, DD-018, DD-022; REQ-200.b).

A wheel build exports the standalone Reflex frontend with the same code as
``uv run python -m reflex_agentlens._webapp.build`` (Bun 1.4.4 or Node needed at build time
only), writes ``csp.json`` next to it and force-includes the result as
``reflex_agentlens/_frontend/``. Installed wheels then run with no JS runtime.

- Editable installs (``uv sync``) skip the export, so syncing needs no JS runtime.
- ``AGENTLENS_SKIP_FRONTEND=1`` builds a wheel without the frontend (fast CI jobs); ``serve``
  then answers ``/`` with the build hint.
- The isolated build environment does not have this package installed, so ``build.py`` is
  loaded by file path (it imports only the standard library) and the export subprocess gets
  ``src/`` on ``PYTHONPATH`` (spike S1). ``require-runtime-dependencies`` installs what the
  exported app imports.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BUILD_MODULE = Path("src/reflex_agentlens/_webapp/build.py")
WORK_DIR = Path("build/agentlens-wheel")
WHEEL_TARGET = "reflex_agentlens/_frontend"
SKIP_ENV = "AGENTLENS_SKIP_FRONTEND"


def load_build_module(root: Path) -> ModuleType:
    """``reflex_agentlens._webapp.build`` loaded from its file, without importing the package."""
    spec = importlib.util.spec_from_file_location("_agentlens_build", root / BUILD_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {root / BUILD_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def should_export(target: str, version: str, env: dict[str, str]) -> bool:
    """Only standard wheels carry the frontend."""
    return target == "wheel" and version != "editable" and env.get(SKIP_ENV) != "1"


class CustomBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if not should_export(self.target_name, version, dict(os.environ)):
            if self.target_name == "wheel" and version != "editable":
                self.app.display_warning(f"{SKIP_ENV}=1: wheel built without the frontend")
            return
        root = Path(self.root)
        build = load_build_module(root)
        work = root / WORK_DIR
        if work.exists():
            shutil.rmtree(work)
        self.app.display_info("Exporting the agentlens frontend (reflex export) …")
        out = build.export(work / "app", work / "_frontend", mark_ignored=False)
        hashes = build.write_csp(out)
        self.app.display_info(f"Frontend ready: {len(hashes)} inline-script hashes in csp.json")
        build_data["force_include"][str(out)] = WHEEL_TARGET
