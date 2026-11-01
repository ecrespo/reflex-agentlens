"""Operational HTTP routes: ``/healthz`` and ``/readyz`` (plan task P5-01.2; REQ-340, API §4).

``/healthz`` answers "is the process alive": 200 unless a graceful shutdown started. ``/readyz``
answers "should traffic come here": the database answers, the schema is current and, when this
process ingests, the ingest engine accepts. The probes are cached for ``ttl_s`` so a busy load
balancer cannot turn health checks into database load. Bodies carry statuses only: no versions,
no configuration (REQ-340.a).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from reflex_agentlens.store.records import SchemaStatus

__all__ = ["Health", "HealthStore", "health_routes"]

Check = Literal["ok", "fail"]
_NO_STORE = {"Cache-Control": "no-store"}


class HealthStore(Protocol):
    async def ping(self) -> bool: ...
    async def schema_status(self) -> SchemaStatus: ...


class Health:
    """Readiness state of one process; ``shutting_down`` is set when SIGTERM arrives."""

    def __init__(
        self,
        store: HealthStore,
        ingest_ready: Callable[[], bool] | None = None,
        *,
        ttl_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.ingest_ready = ingest_ready  # None: this process does not ingest
        self.ttl_s = ttl_s
        self.clock = clock
        self.shutting_down = False
        self._lock = asyncio.Lock()
        self._at = -float("inf")
        self._database: Check = "fail"
        self._schema: Check = "fail"

    async def _probe(self) -> None:
        async with self._lock:
            if self.clock() - self._at < self.ttl_s:
                return
            up = await self.store.ping()
            self._database = "ok" if up else "fail"
            if up:  # while the database is down, keep the last known schema verdict
                try:
                    current = await self.store.schema_status() is SchemaStatus.CURRENT
                except Exception:  # a probe never raises; the next one retries
                    self._database = "fail"
                else:
                    self._schema = "ok" if current else "fail"
            self._at = self.clock()

    async def checks(self) -> dict[str, Check]:
        await self._probe()
        result: dict[str, Check] = {"database": self._database, "schema": self._schema}
        if self.ingest_ready is not None:
            result["ingest"] = "ok" if self.ingest_ready() and not self.shutting_down else "fail"
        return result

    async def ready(self) -> bool:
        return not self.shutting_down and all(v == "ok" for v in (await self.checks()).values())


def health_routes(current: Callable[[], Health | None]) -> list[Route]:
    """``current`` returns the process's :class:`Health`, or ``None`` before startup finished
    (both endpoints then answer 503 ``starting``)."""

    async def healthz(_: Request) -> JSONResponse:
        health = current()
        if health is None:
            return JSONResponse({"status": "starting"}, 503, headers=_NO_STORE)
        if health.shutting_down:
            return JSONResponse({"status": "shutting_down"}, 503, headers=_NO_STORE)
        return JSONResponse({"status": "ok"}, headers=_NO_STORE)

    async def readyz(_: Request) -> JSONResponse:
        health = current()
        if health is None:
            return JSONResponse({"status": "starting"}, 503, headers=_NO_STORE)
        checks = await health.checks()
        if not health.shutting_down and all(v == "ok" for v in checks.values()):
            return JSONResponse({"status": "ready"}, headers=_NO_STORE)
        body = {"status": "not_ready", "checks": checks}
        return JSONResponse(body, 503, headers=_NO_STORE)

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
    ]
