"""Ingest settings cache against both backends (plan task P3-07.2; REQ-006.c, DD-007)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from reflex_agentlens.core.config import load_config
from reflex_agentlens.core.errors import NotFound
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.ingest.jobs import JobStore, SettingsCache
from reflex_agentlens.store.records import NewProject, ProjectPatch, RedactionRuleSet
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.test_settings import CLAUDE, GPT


async def make_cache(db: ContractDB, tmp_path: Path) -> tuple[SettingsCache, str]:
    backend = db.require_backend()
    project = await backend.create_project(NewProject(new_ulid(), "demo", "Demo"))
    cache = SettingsCache(cast(JobStore, backend), load_config(env={}, cwd=tmp_path))
    return cache, project.id


async def test_settings_change_reaches_the_normalizer(db: ContractDB, tmp_path: Path) -> None:
    """A ``settings_changed`` event drops the entry; the next batch sees the new settings."""
    backend = db.require_backend()
    cache, pid = await make_cache(db, tmp_path)
    ctx = await cache.context(pid, NOW_NS)
    assert ctx.content_storage
    assert [r.name for r in ctx.rules.patterns] == ["secrets"]  # the default preset
    assert ctx.retention_max_age_s == 2_592_000
    assert len(ctx.session_secret) == 32

    watcher = asyncio.create_task(cache.watch())
    await asyncio.sleep(0.05)  # subscribed
    try:
        rules = RedactionRuleSet({"email": True}, [], [])
        await backend.save_redaction_rules(pid, rules)
        await backend.update_project(
            pid, ProjectPatch(content_storage=False, retention_max_age_s=60)
        )
        async with asyncio.timeout(3):
            while (await cache.context(pid, NOW_NS)).content_storage:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
    finally:
        watcher.cancel()
    ctx = await cache.context(pid, NOW_NS)
    assert not ctx.content_storage
    assert ctx.retention_max_age_s == 60
    assert [r.name for r in ctx.rules.patterns] == ["email"]


async def test_refresh_picks_up_pricing_and_deleted_projects(
    db: ContractDB, tmp_path: Path
) -> None:
    backend = db.require_backend()
    cache, pid = await make_cache(db, tmp_path)
    await cache.refresh()
    assert cache.pricing.at(NOW_NS) is None
    await backend.save_pricing([GPT], "ana", effective_from_ns=0)
    await backend.save_pricing([GPT, CLAUDE], "ana", effective_from_ns=NOW_NS + 100)
    await cache.refresh()  # pricing changes have no event: the periodic refresh picks them up
    old, new = cache.pricing.at(NOW_NS), cache.pricing.at(NOW_NS + 100)
    assert old is not None
    assert new is not None
    assert (len(old.entries), len(new.entries)) == (1, 2)

    await cache.project(pid)
    await backend.delete_project(pid)
    cache.invalidate(pid)
    with pytest.raises(NotFound):
        await cache.context(pid, NOW_NS)
