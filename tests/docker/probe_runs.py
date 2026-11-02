"""Run inside a container (``python - < probe_runs.py``): print the agent names of the Runs the
run-list query returns (plan task P7-03.1; REQ-204, 210.a, 364). Uses the container's own
configuration, so it reads the same database as the running server."""

from __future__ import annotations

import asyncio
import json

from reflex_agentlens.app.backend import open_backend
from reflex_agentlens.core.config import load_config
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.query.filters import RunFilter
from reflex_agentlens.query.service import QueryService


async def main() -> None:
    cfg = load_config()
    backend = await open_backend(cfg)
    try:
        query = QueryService(backend, b"probe-cursor-key-not-a-secret!!!")
        page = await query.list_runs(Viewer("probe", "probe", is_admin=True), RunFilter())
        print(json.dumps([run.model_dump(mode="json") for run in page.items]))  # noqa: T201
    finally:
        await backend.close()


asyncio.run(main())
