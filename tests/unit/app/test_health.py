"""Health probe caching (plan task P5-01.2; REQ-340.a)."""

from __future__ import annotations

from reflex_agentlens.app.routes import Health
from reflex_agentlens.store.records import SchemaStatus


class Store:
    def __init__(self) -> None:
        self.up = True
        self.status = SchemaStatus.CURRENT
        self.error: Exception | None = None
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        return self.up

    async def schema_status(self) -> SchemaStatus:
        if self.error is not None:
            raise self.error
        return self.status


async def test_probes_are_cached_for_the_ttl() -> None:
    store, now = Store(), [0.0]
    health = Health(store, lambda: True, clock=lambda: now[0])
    assert await health.checks() == {"database": "ok", "schema": "ok", "ingest": "ok"}
    store.up = False
    now[0] = 0.5
    assert await health.ready()  # cached
    assert store.pings == 1
    now[0] = 1.5
    assert await health.checks() == {"database": "fail", "schema": "ok", "ingest": "ok"}
    assert store.pings == 2


async def test_outdated_schema_and_ingest_and_shutdown() -> None:
    store = Store()
    store.status = SchemaStatus.OUTDATED
    ingest = [False]
    health = Health(store, lambda: ingest[0], ttl_s=0)
    assert await health.checks() == {"database": "ok", "schema": "fail", "ingest": "fail"}
    store.status = SchemaStatus.CURRENT
    ingest[0] = True
    assert await health.ready()
    health.shutting_down = True
    assert not await health.ready()
    assert (await health.checks())["ingest"] == "fail"


async def test_a_failing_schema_probe_counts_as_database_down() -> None:
    store = Store()
    store.error = RuntimeError("lost")
    health = Health(store, ttl_s=0)
    assert await health.checks() == {"database": "fail", "schema": "fail"}  # no ingest key
