"""Hatchling build hook: export the Reflex frontend and ship it inside the wheel.

Spike prototype (Tasks P0-02.2, Tech Design DD-002). The real hook is P7-01.

Runs `reflex export --frontend-only --no-zip` against the build-only app module
(`reflex_agentlens._webapp`) using the rxconfig in `spikes/s1/`, then force-includes
`.web/build/client/**` as `reflex_agentlens/_frontend/` in the wheel. Editable installs
(`uv sync`) skip the export so that day-to-day syncs stay fast and need no JS runtime.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

EXPORT_DIR = Path("spikes/s1")
CLIENT_DIR = EXPORT_DIR / ".web" / "build" / "client"
WHEEL_TARGET = "reflex_agentlens/_frontend"


class CustomBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        if os.environ.get("AGENTLENS_SKIP_FRONTEND") == "1":
            self.app.display_warning("AGENTLENS_SKIP_FRONTEND=1: wheel built without frontend")
            return
        root = Path(self.root)
        export_dir = root / EXPORT_DIR
        reflex_bin = shutil.which("reflex") or str(Path(sys.executable).parent / "reflex")
        self.app.display_info(f"Exporting Reflex frontend in {export_dir} …")
        # The isolated build env does not have this package installed: expose src/ to the export.
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(root / "src"), env.get("PYTHONPATH", "")) if p
        )
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [reflex_bin, "export", "--frontend-only", "--no-zip", "--loglevel", "warning"],
            cwd=export_dir,
            env=env,
            check=True,
        )
        client = root / CLIENT_DIR
        if not (client / "index.html").is_file():
            raise RuntimeError(f"frontend export produced no {client / 'index.html'}")
        build_data["force_include"][str(client)] = WHEEL_TARGET
