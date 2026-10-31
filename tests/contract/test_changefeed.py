"""Change-feed contract tests (plan tasks P2-10.1, P2-10.2; REQ-140.a, REQ-205.c, DD-007).

Events are published after the commit and never for a rolled-back transaction; ``run_keys`` is
capped at 200. On PostgreSQL a second process's commit reaches this process within 3 s through
LISTEN/NOTIFY, own events are not delivered twice, and polling covers a lost LISTEN connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from reflex_agentlens.core.security import generate_api_key, hash_secret
from reflex_agentlens.store.changefeed import Subscription
from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.records import (
    ChangeEvent,
    NewApiKeyRecord,
    ProjectPatch,
    RedactionRuleSet,
    RunKey,
    RunQuery,
)
from reflex_agentlens.store.writepath import write_batch as real_write_batch
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.test_reads import all_runs
from tests.contract.test_write_batch import WINDOW, span, write

DELIVERY_S = 3.0  # REQ-140.a / REQ-205.c


def state(backend: PostgresBackend) -> str:
    return backend.listener_state  # read through a call: the test changes it from outside


def trace(n: int) -> bytes:
    return n.to_bytes(16, "big")


async def receive(sub: Subscription, within: float = DELIVERY_S) -> ChangeEvent:
    return await asyncio.wait_for(anext(sub), within)


async def nothing_more(sub: Subscription, wait: float = 0.3) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(sub), wait)


async def test_write_batch_publishes_after_commit(db: ContractDB) -> None:
    backend = db.require_backend()
    project, other = await db.make_project(), await db.make_project()
    async with backend.changes.subscribe(frozenset({project})) as sub:
        await write(db, other, span(9, 1, None, 10, 5))  # another Project: filtered out
        await write(db, project, span(1, 1, None, 10, 5), span(2, 1, None, 10, 5))
        ev = await receive(sub)
        assert (ev.kind, ev.project_id, ev.truncated) == ("spans_committed", project, False)
        assert ev.run_keys == (RunKey(project, trace(1)), RunKey(project, trace(2)))
        assert ev.committed_at_ns == NOW_NS
        # the event is published after the commit: a re-query sees the data
        assert len(await all_runs(db, RunQuery(frozenset({project})))) == 2
        await write(db, project, span(1, 1, None, 10, 5))  # duplicate only: nothing changed
        await nothing_more(sub)


async def test_rolled_back_write_publishes_nothing(
    db: ContractDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = db.require_backend()
    project = await db.make_project()

    def fail_after_writing(*args: Any, **kwargs: Any) -> Any:
        real_write_batch(*args, **kwargs)
        raise RuntimeError("crash before commit")

    async with backend.changes.subscribe() as sub:
        monkeypatch.setattr("reflex_agentlens.store.writeapi.write_batch", fail_after_writing)
        with pytest.raises(RuntimeError):
            await write(db, project, span(1, 1, None, 10, 5))
        monkeypatch.undo()
        await nothing_more(sub)
        assert await all_runs(db, RunQuery(frozenset({project}))) == []
        await write(db, project, span(1, 1, None, 10, 5))
        assert (await receive(sub)).run_keys == (RunKey(project, trace(1)),)


async def test_run_keys_are_capped(db: ContractDB) -> None:
    backend = db.require_backend()
    project = await db.make_project()
    async with backend.changes.subscribe() as sub:
        await write(db, project, *(span(t, 1, None, 10, 5) for t in range(1, 251)))
        ev = await receive(sub)
        assert (len(ev.run_keys), ev.truncated) == (200, True)


async def test_finalizer_purge_and_settings_publish(db: ContractDB) -> None:
    backend = db.require_backend()
    project = await db.make_project()
    await write(db, project, span(1, 2, 1, 70, 60), span(2, 1, None, 40 * 86_400, 40 * 86_400))
    key = generate_api_key()
    h = hash_secret(key.secret)
    await backend.create_api_key(NewApiKeyRecord(key.key_id, project, h.salt, h.digest, NOW_NS))

    async with backend.changes.subscribe() as sub:
        db.clock.now_ns = NOW_NS + WINDOW
        await backend.finalize_stale_runs(db.clock.now_ns - WINDOW, 500)
        ev = await receive(sub)
        assert (ev.kind, ev.run_keys) == ("runs_finalized", (RunKey(project, trace(1)),))

        await backend.purge_by_age(db.clock.now_ns, batch_traces=10)
        ev = await receive(sub)
        assert (ev.kind, ev.run_keys) == ("purged", (RunKey(project, trace(2)),))

        actions: list[Callable[[], Any]] = [
            lambda: backend.revoke_api_key(key.key_id, NOW_NS),  # REQ-006.c
            lambda: backend.update_project(project, ProjectPatch(name="renamed")),
            lambda: backend.save_redaction_rules(project, RedactionRuleSet({}, [], [])),
            lambda: backend.delete_project(project),
        ]
        for action in actions:
            await action()
            ev = await receive(sub)
            assert (ev.kind, ev.project_id, ev.run_keys) == ("settings_changed", project, ())
        await nothing_more(sub)


async def test_start_change_listener_is_idempotent(db: ContractDB) -> None:
    backend = db.require_backend()
    await backend.start_change_listener()
    await backend.start_change_listener()


# ── PostgreSQL: across processes (P2-10.2) ───────────────────────────────────────────────────


async def _second(db: ContractDB, **kwargs: Any) -> PostgresBackend:
    if db.name != "pg":
        pytest.skip("cross-process delivery is a PostgreSQL concern (DD-007)")
    assert isinstance(db.backend, PostgresBackend)
    other = PostgresBackend(db.backend.url, search_path=db.backend.search_path, pool_size=2,
                            **kwargs)  # fmt: skip
    await other.open()
    return other


@pytest.fixture
async def closing() -> AsyncIterator[list[PostgresBackend]]:
    backends: list[PostgresBackend] = []
    yield backends
    for b in backends:
        await b.close()


@pytest.mark.pg
async def test_other_process_write_is_delivered(
    db: ContractDB, closing: list[PostgresBackend]
) -> None:
    """REQ-205.c: a second process's commit reaches this process within 3 s, exactly once."""
    reader = await _second(db)
    closing.append(reader)
    writer = db.require_backend()
    await reader.start_change_listener()
    assert state(reader) == "listening"
    project = await db.make_project()
    async with reader.changes.subscribe() as theirs, writer.changes.subscribe() as mine:
        await write(db, project, span(1, 1, None, 10, 5))
        ev = await receive(theirs)
        assert (ev.kind, ev.run_keys) == ("spans_committed", (RunKey(project, trace(1)),))
        ev = await receive(mine)  # published locally after commit
        assert ev.run_keys == (RunKey(project, trace(1)),)
        await nothing_more(theirs)
        await nothing_more(mine)

    # own events are not echoed back through LISTEN
    await writer.start_change_listener()
    async with writer.changes.subscribe() as mine:
        await write(db, project, span(2, 1, None, 10, 5))
        assert (await receive(mine)).run_keys == (RunKey(project, trace(2)),)
        await nothing_more(mine, wait=0.5)


@pytest.mark.pg
async def test_other_schema_events_are_ignored(
    db: ContractDB, closing: list[PostgresBackend]
) -> None:
    reader = await _second(db)
    closing.append(reader)
    reader.search_path = "another_deployment"  # same database and channel, other namespace
    await reader.start_change_listener()
    project = await db.make_project()
    async with reader.changes.subscribe() as sub:
        await write(db, project, span(1, 1, None, 10, 5))
        await nothing_more(sub, wait=0.5)


@pytest.mark.pg
async def test_polling_covers_a_lost_listen_connection(
    db: ContractDB, closing: list[PostgresBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DD-007: LISTEN connection killed → polling delivers, then LISTEN resumes."""
    reader = await _second(db, poll_interval_s=0.2)
    closing.append(reader)
    await reader.start_change_listener()
    conn = reader._listen_conn
    assert conn is not None

    down = True
    real_connect = reader._connect_listener

    async def maybe_connect() -> Any:
        if down:
            raise OSError("still down")
        return await real_connect()

    monkeypatch.setattr(reader, "_connect_listener", maybe_connect)
    async with db.engine.begin() as admin:
        await admin.exec_driver_sql(f"SELECT pg_terminate_backend({conn.get_server_pid()})")
    for _ in range(50):
        if state(reader) == "polling":
            break
        await asyncio.sleep(0.1)
    assert state(reader) == "polling"

    project = await db.make_project()
    async with reader.changes.subscribe(frozenset({project})) as sub:
        await write(db, project, span(1, 1, None, 10, 5))
        ev = await receive(sub)  # found by polling: "something changed, re-query"
        assert (ev.project_id, ev.truncated, ev.run_keys) == (project, True, ())

        down = False
        for _ in range(50):
            if state(reader) == "listening":
                break
            await asyncio.sleep(0.1)
        assert state(reader) == "listening"
        await asyncio.sleep(0.3)  # the poller's baseline is refreshed after reconnecting
        while not sub._queue.empty():
            sub._queue.get_nowait()
        await write(db, project, span(2, 1, None, 10, 5))
        ev = await receive(sub)
        assert ev.run_keys == (RunKey(project, trace(2)),)  # via NOTIFY again


@pytest.mark.pg
async def test_listener_starts_in_polling_when_listen_fails(
    db: ContractDB, closing: list[PostgresBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = await _second(db, poll_interval_s=0.2)
    closing.append(reader)

    async def refuse() -> Any:
        raise OSError("refused")

    monkeypatch.setattr(reader, "_connect_listener", refuse)
    await reader.start_change_listener()
    assert state(reader) == "polling"
    await reader.close()
    assert state(reader) == "stopped"
