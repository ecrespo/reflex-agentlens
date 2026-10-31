"""Project and API key contract tests (plan task P2-08.1; Data Model §2.2-2.4, Q14).

REQ-170 (storage side), REQ-320.a (no plaintext secret at rest), REQ-006.c (settings_version).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from reflex_agentlens.core.errors import Conflict, NotFound, ValidationError
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.security import (
    SecretHash,
    generate_api_key,
    hash_secret,
    verify_secret,
)
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.admin import TOUCH_INTERVAL_NS
from reflex_agentlens.store.records import (
    NewApiKeyRecord,
    NewProject,
    ProjectPatch,
    RunQuery,
)
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.dataset import dataset
from tests.contract.test_reads import all_runs, load


async def make(db: ContractDB, slug: str, name: str | None = None) -> str:
    project = await db.require_backend().create_project(
        NewProject(id=new_ulid(), slug=slug, name=name or slug.title(), created_by="admin")
    )
    return project.id


async def test_project_lifecycle(db: ContractDB) -> None:
    """REQ-170.a: defaults; update bumps settings_version; list ordered by slug."""
    backend = db.require_backend()
    pid = await make(db, "support-bots", "Support bots")
    await make(db, "alpha")
    projects = await backend.list_projects()
    assert [p.slug for p in projects] == ["alpha", "support-bots"]
    p = next(p for p in projects if p.id == pid)
    assert (p.name, p.content_storage, p.retention_max_age_s) == ("Support bots", True, 2_592_000)
    assert (p.request_rate, p.request_burst, p.span_rate, p.span_burst) == (50, 100, 20000, 40000)
    assert (p.settings_version, p.created_at_ns, p.updated_by) == (1, NOW_NS, "admin")
    rules = await db.scalar(
        "SELECT count(*) FROM redaction_rule_sets r JOIN projects p ON p.pk = r.project_pk "
        "WHERE p.id = :pid",
        pid=pid,
    )
    assert rules == 1

    db.clock.now_ns += 5
    updated = await backend.update_project(
        pid, ProjectPatch(name="Support", content_storage=False, span_rate=10, updated_by="ana")
    )
    assert (updated.name, updated.content_storage, updated.span_rate) == ("Support", False, 10)
    assert (updated.request_rate, updated.settings_version) == (50, 2)
    assert (updated.updated_at_ns, updated.updated_by) == (NOW_NS + 5, "ana")
    assert (await backend.update_project(pid, ProjectPatch())).settings_version == 3


@pytest.mark.parametrize(
    "slug", ["A-upper", "-lead", "trail-", "ab", "x" * 41, "spaces here", "under_score"]
)
async def test_invalid_slug(db: ContractDB, slug: str) -> None:
    with pytest.raises(ValidationError):
        await make(db, slug)


async def test_conflicts_and_validation(db: ContractDB) -> None:
    backend = db.require_backend()
    pid = await make(db, "shop")
    with pytest.raises(Conflict):
        await make(db, "shop", "Another name")
    with pytest.raises(ValidationError):
        await make(db, "blank", "   ")
    with pytest.raises(ValidationError):
        await backend.update_project(pid, ProjectPatch(request_rate=0))
    with pytest.raises(ValidationError):
        await backend.update_project(pid, ProjectPatch(retention_max_age_s=-1))
    with pytest.raises(NotFound):
        await backend.update_project("01NOPROJECT", ProjectPatch(name="x"))
    with pytest.raises(NotFound):
        await backend.delete_project("01NOPROJECT")


async def test_delete_project_cascades(db: ContractDB) -> None:
    """REQ-170.d: every row of the Project goes; other Projects are untouched."""
    backend = db.require_backend()
    runs = dataset(3, 400)
    doomed, kept = await load(db, runs), await load(db, runs)
    key = generate_api_key()
    h = hash_secret(key.secret)
    await backend.create_api_key(NewApiKeyRecord(key.key_id, doomed, h.salt, h.digest, NOW_NS))
    await backend.delete_project(doomed)
    assert doomed not in {p.id for p in await backend.list_projects()}
    for table in ("runs", "spans", "sessions", "sources", "resources", "llm_calls", "api_keys"):
        left = await db.scalar(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
        assert left == (
            0 if table == "api_keys" else await db.scalar(
                f"SELECT count(*) FROM {table} WHERE project_pk = "  # noqa: S608
                "(SELECT pk FROM projects WHERE id = :p)",
                p=kept,
            )
        ), table  # fmt: skip
    assert len(await all_runs(db, RunQuery(frozenset({kept})))) == len(runs)
    assert await backend.resolve_api_key(key.key_id) is None


async def test_api_key_records(db: ContractDB) -> None:
    """REQ-170.b/c, Q14: create, resolve, list, revoke (terminal), each bumping the version."""
    backend = db.require_backend()
    pid = await make(db, "keys")
    key = generate_api_key()
    h = hash_secret(key.secret)
    rec = await backend.create_api_key(
        NewApiKeyRecord(key.key_id, pid, h.salt, h.digest, NOW_NS, label="prod", created_by="cli")
    )
    assert (rec.key_id, rec.project_id, rec.label, rec.created_by) == (
        key.key_id, pid, "prod", "cli",
    )  # fmt: skip
    assert (rec.last_used_at_ns, rec.revoked_at_ns) == (None, None)
    assert verify_secret(key.secret, SecretHash(rec.salt, rec.secret_hash))
    assert key.secret not in repr(rec)
    assert await backend.resolve_api_key(key.key_id) == rec
    assert await backend.resolve_api_key("zzzzzzzz") is None
    assert await backend.list_api_keys(pid) == [rec]
    projects = {p.id: p for p in await backend.list_projects()}
    assert projects[pid].settings_version == 2
    with pytest.raises(Conflict):
        await backend.create_api_key(NewApiKeyRecord(key.key_id, pid, h.salt, h.digest, NOW_NS))
    with pytest.raises(NotFound):
        await backend.create_api_key(NewApiKeyRecord("aaaaaaaa", "01NO", h.salt, h.digest, 0))
    with pytest.raises(ValidationError):
        await backend.create_api_key(NewApiKeyRecord("bbbbbbbb", pid, b"x", h.digest, 0))
    with pytest.raises(NotFound):
        await backend.list_api_keys("01NO")

    await backend.revoke_api_key(key.key_id, NOW_NS + 1)
    await backend.revoke_api_key(key.key_id, NOW_NS + 2)  # idempotent, keeps the first time
    revoked = await backend.resolve_api_key(key.key_id)
    assert revoked is not None
    assert revoked.revoked_at_ns == NOW_NS + 1
    projects = {p.id: p for p in await backend.list_projects()}
    assert projects[pid].settings_version == 3
    with pytest.raises(NotFound):
        await backend.revoke_api_key("zzzzzzzz", NOW_NS)


async def test_touch_is_throttled_to_once_a_minute(db: ContractDB) -> None:
    backend = db.require_backend()
    pid = await make(db, "touchy")
    key = generate_api_key()
    h = hash_secret(key.secret)
    await backend.create_api_key(NewApiKeyRecord(key.key_id, pid, h.salt, h.digest, NOW_NS))

    async def last_used() -> int | None:
        rec = await backend.resolve_api_key(key.key_id)
        assert rec is not None
        return rec.last_used_at_ns

    await backend.touch_api_key(key.key_id, NOW_NS)
    assert await last_used() == NOW_NS
    await backend.touch_api_key(key.key_id, NOW_NS + TOUCH_INTERVAL_NS - 1)
    assert await last_used() == NOW_NS
    await backend.touch_api_key(key.key_id, NOW_NS + TOUCH_INTERVAL_NS)
    assert await last_used() == NOW_NS + TOUCH_INTERVAL_NS
    await backend.touch_api_key("zzzzzzzz", NOW_NS)  # unknown keys are ignored


async def test_no_plaintext_secret_at_rest(db: ContractDB) -> None:
    """REQ-320.a canary: after create, resolve, touch and revoke, no column holds the secret."""
    backend = db.require_backend()
    pid = await make(db, "canary")
    key = generate_api_key()
    h = hash_secret(key.secret)
    await backend.create_api_key(
        NewApiKeyRecord(key.key_id, pid, h.salt, h.digest, NOW_NS, label="canary")
    )
    await backend.resolve_api_key(key.key_id)
    await backend.touch_api_key(key.key_id, NOW_NS)
    await backend.revoke_api_key(key.key_id, NOW_NS)
    needles = (key.secret, key.plaintext)
    async with db.engine.connect() as conn:
        for table in s.metadata.sorted_tables:
            for row in (await conn.execute(select(table))).all():
                for value in row:
                    text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
                    assert not any(n in text for n in needles), table.name
