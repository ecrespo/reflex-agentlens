"""Spike S1b: boot the pre-built frontend from an installed wheel (Tasks P0-03.1, P0-03.2).

Usage: python boot.py --data-dir DIR [--host 127.0.0.1] [--port 8000]

Implements the runtime recipe of Tech Design DD-002 with no JS runtime:
1. copy the packaged bundle (reflex_agentlens/_frontend) to {data_dir}/web/<version>/.web/build/client
2. rewrite the compiled env module when the port differs from the baked one (P0-03.2)
3. write a minimal rxconfig.py (telemetry off) in {data_dir}/web/<version>
4. set the Reflex env vars and run Reflex's own prod backend runner (Granian, 1 worker)
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

BAKED_ORIGIN = "localhost:8000"

RXCONFIG = """import reflex as rx

config = rx.Config(
    app_name="agentlens_webapp",
    app_module_import="reflex_agentlens._webapp.app",
    telemetry_enabled=False,
    show_built_with_reflex=False,
    plugins=[],
)
"""


def prepare_workdir(data_dir: Path, port: int) -> Path:
    import reflex_agentlens

    version_dir = data_dir / "web" / reflex_agentlens.__version__
    client = version_dir / ".web" / "build" / "client"
    if client.exists():
        shutil.rmtree(client)
    bundle = resources.files("reflex_agentlens") / "_frontend"
    with resources.as_file(bundle) as src:
        shutil.copytree(src, client)
    (version_dir / "rxconfig.py").write_text(RXCONFIG)
    rewrite_env_module(client, port)
    return version_dir


def rewrite_env_module(client: Path, port: int) -> None:
    """Point the compiled backend URLs at the runtime port (hostname is resolved by the client)."""
    if port == 8000:
        return
    for js in (client / "assets").glob("reflex-env-*.js"):
        text = js.read_text()
        new = text.replace(BAKED_ORIGIN, f"localhost:{port}")
        js.write_text(new)
        # Precompressed siblings would be served instead of the rewritten file.
        for suffix in (".gz", ".br", ".zst"):
            sib = js.with_name(js.name + suffix)
            if sib.exists():
                sib.unlink()
        gz = js.with_name(js.name + ".gz")
        gz.write_bytes(gzip.compress(new.encode()))
        n = len(re.findall(re.escape(f"localhost:{port}"), new))
        print(f"[boot] rewrote {js.name}: {n} URLs -> port {port}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    t0 = time.monotonic()
    version_dir = prepare_workdir(args.data_dir.resolve(), args.port)
    os.chdir(version_dir)
    sys.path.insert(0, str(version_dir))
    os.environ.update(
        {
            "REFLEX_WEB_WORKDIR": str(version_dir / ".web"),
            "REFLEX_STATES_WORKDIR": str(version_dir / ".states"),
            "REFLEX_ENV_MODE": "prod",
            "__REFLEX_MOUNT_FRONTEND_COMPILED_APP": "true",
            "__REFLEX_SKIP_COMPILE": "true",
            "GRANIAN_WORKERS": "1",
            "REFLEX_TELEMETRY_ENABLED": "false",
        }
    )
    print(f"[boot] workdir ready in {time.monotonic() - t0:.2f}s: {version_dir}", flush=True)

    from reflex.utils import exec as rx_exec
    from reflex_base import constants

    rx_exec.run_backend_prod(
        args.host, args.port, constants.LogLevel.INFO, mount_frontend_compiled_app=True
    )


if __name__ == "__main__":
    main()
