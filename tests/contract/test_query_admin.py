"""``AdminService``, subscriptions and demo mode on both backends (plan tasks P4-05.1, P4-05.2,
P4-06.1; API §6.3-6.5).

REQ-170 (Projects, keys), REQ-171 (pricing, CSV, unpriced), REQ-172 (redaction rules, preview),
REQ-173 (settings), REQ-174 (admin gating), REQ-102.a (saved filters), REQ-062 (recompute),
REQ-140 (coalesced notices), REQ-231.b (demo mode), REQ-191.c (scope).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from reflex_agentlens.core.config import AgentlensConfig, load_config
from reflex_agentlens.core.errors import Conflict, Forbidden, NotFound, ValidationError
from reflex_agentlens.core.security import SecretHash, parse_api_key, verify_secret
from reflex_agentlens.query import dto
from reflex_agentlens.query.admin import AdminService
from reflex_agentlens.query.service import QueryService
from reflex_agentlens.store.records import ChangeEvent, NormalizedBatch, RunKey
from tests.contract.conftest import ContractDB
from tests.contract.dataset import BASE_NS, NS, dataset

ADMIN = dto.Viewer("admin", "Admin", is_admin=True)
USER = dto.Viewer("user", "User", is_admin=False)
RULES = dto.RedactionRuleSet(
    presets={"secrets": True, "email": True, "phone": False, "credit_card": False},
    pattern_rules=[dto.PatternRule(name="ticket", regex=r"TCK-\d+")],
    field_rules=[dto.FieldRule(key_glob="*.password", action="drop")],
)


def config(tmp_path: Path, **overrides: object) -> AgentlensConfig:
    return load_config(env={}, cwd=tmp_path, cli_overrides=overrides)


def admin(db: ContractDB, tmp_path: Path, **overrides: object) -> AdminService:
    return AdminService(db.require_backend(), config(tmp_path, **overrides), clock_ns=db.clock)


async def test_admin_gating_req_174(db: ContractDB, tmp_path: Path) -> None:
    svc = admin(db, tmp_path)
    project = await svc.create_project(ADMIN, "Billing", "billing")
    calls = [
        svc.list_projects_admin(USER),
        svc.create_project(USER, "X", "x-proj"),
        svc.update_project_settings(USER, project.id, dto.ProjectSettingsPatch(name="Y")),
        svc.delete_project(USER, project.id, "Billing"),
        svc.create_api_key(USER, project.id, "ci"),
        svc.list_api_keys(USER, project.id),
        svc.get_pricing(USER),
        svc.list_pricing_versions(USER),
        svc.save_pricing(USER, []),
        svc.list_unpriced_models(USER),
        svc.export_pricing_csv(USER),
        svc.get_redaction_rules(USER, project.id),
        svc.save_redaction_rules(USER, project.id, RULES),
        svc.recompute_costs(USER, project.id, "now-1d"),
    ]
    for call in calls:
        with pytest.raises(Forbidden):
            await call
    with pytest.raises(Forbidden):
        svc.preview_redaction(USER, RULES, "x")
    limited = dto.Viewer("a2", "A2", is_admin=True, allowed_project_ids=frozenset({"other"}))
    with pytest.raises(Forbidden):
        await svc.list_api_keys(limited, project.id)
    with pytest.raises(Forbidden):
        await svc.create_project(limited, "Z", "z-proj")
    with pytest.raises(Forbidden):  # global pricing needs a full-scope admin
        await svc.save_pricing(limited, [])
    with pytest.raises(Forbidden):
        await svc.import_pricing_csv(limited, "")


async def test_projects_req_170_173(db: ContractDB, tmp_path: Path) -> None:
    svc = admin(
        db, tmp_path, **{"ingest.default_request_rate": 7, "retention.default_max_age": "10d"}
    )
    project = await svc.create_project(ADMIN, "Billing bot", "billing-bot")
    assert (project.retention_days, project.content_storage, project.request_rate) == (10, True, 7)
    with pytest.raises(Conflict):
        await svc.create_project(ADMIN, "Again", "billing-bot")
    patched = await svc.update_project_settings(
        ADMIN, project.id, dto.ProjectSettingsPatch(retention_days=0, content_storage=False)
    )
    assert (patched.retention_days, patched.content_storage) == (0, False)
    assert patched.settings_version > project.settings_version
    assert [p.id for p in await svc.list_projects_admin(ADMIN)] == [project.id]
    with pytest.raises(ValidationError) as exc:
        await svc.delete_project(ADMIN, project.id, "billing bot")
    assert exc.value.field == "confirm_name"
    await svc.delete_project(ADMIN, project.id, "Billing bot")
    assert await svc.list_projects_admin(ADMIN) == []
    with pytest.raises(NotFound):
        await svc.delete_project(ADMIN, project.id, "Billing bot")


async def test_api_keys_req_170bc(db: ContractDB, tmp_path: Path) -> None:
    svc = admin(db, tmp_path, **{"server.public_url": "https://lens.example.com"})
    project = await svc.create_project(ADMIN, "P", "proj")
    created = await svc.create_api_key(ADMIN, project.id, "ci")
    plaintext = created.plaintext.get_secret_value()
    key_id, secret = parse_api_key(plaintext)
    assert key_id == created.key_id
    assert plaintext not in repr(created)
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT="https://lens.example.com"' in created.snippet
    assert f"Bearer%20{plaintext}" in created.snippet
    record = await db.require_backend().resolve_api_key(key_id)
    assert record is not None
    assert verify_secret(secret, SecretHash(record.salt, record.secret_hash))
    keys = await svc.list_api_keys(ADMIN, project.id)
    assert [(k.key_id, k.prefix, k.label, k.revoked) for k in keys] == [
        (key_id, f"al_{key_id}_", "ci", False)
    ]
    assert plaintext not in str([k.model_dump() for k in keys])
    await svc.revoke_api_key(ADMIN, key_id)
    assert (await svc.list_api_keys(ADMIN, project.id))[0].revoked
    with pytest.raises(NotFound):
        await svc.revoke_api_key(ADMIN, "zzzzzzzz")
    with pytest.raises(ValidationError):
        await svc.create_api_key(ADMIN, project.id, "x" * 101)
    with pytest.raises(NotFound):
        await svc.create_api_key(ADMIN, "missing", "ci")


def entry(
    pattern: str, price: str, kind: str = "exact", provider: str = ""
) -> dto.PricingEntryView:
    return dto.PricingEntryView(
        provider=provider, model_pattern=pattern, match_kind=kind,  # type: ignore[arg-type]
        input_usd_per_mtok=Decimal(price), output_usd_per_mtok=Decimal(price) * 4,
    )  # fmt: skip


async def test_pricing_and_csv_req_171(db: ContractDB, tmp_path: Path) -> None:
    svc = admin(db, tmp_path)
    first = await svc.save_pricing(ADMIN, [entry("gpt-4o", "2.5"), entry("claude-*", "3", "glob")])
    second = await svc.save_pricing(ADMIN, [entry("gpt-4o", "2")], note="cheaper")
    assert second.info.version == first.info.version + 1
    assert (await svc.get_pricing(ADMIN)).info.version == second.info.version
    old = await svc.get_pricing(ADMIN, first.info.version)
    assert [e.model_pattern for e in old.entries] == ["claude-*", "gpt-4o"]  # by pattern
    assert old.entries[1].input_usd_per_mtok == Decimal("2.5")
    versions = await svc.list_pricing_versions(ADMIN)
    assert [v.version for v in versions] == [second.info.version, first.info.version]
    with pytest.raises(NotFound):
        await svc.get_pricing(ADMIN, 999)
    bad = [
        [entry("gpt-*", "1")],  # * in an exact pattern
        [entry("gpt", "1", "glob")],  # glob without *
        [entry("m", "1"), entry("m", "2")],  # duplicate
        [entry(" ", "1")],
    ]
    for entries in bad:
        with pytest.raises(ValidationError):
            await svc.save_pricing(ADMIN, entries)
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic rejects negative prices
        entry("m", "-1")
    csv_text = await svc.export_pricing_csv(ADMIN)
    imported = await svc.import_pricing_csv(ADMIN, csv_text)
    assert imported.info.source == "csv"
    assert imported.entries == (await svc.get_pricing(ADMIN, second.info.version)).entries
    with pytest.raises(ValidationError):
        await svc.import_pricing_csv(ADMIN, "not,a,pricing,csv\n")


async def test_unpriced_models_and_recompute_req_171b_062(db: ContractDB, tmp_path: Path) -> None:
    svc = admin(db, tmp_path)
    project = await db.make_project("costs")
    runs = dataset(3, 300)
    await db.require_backend().write_batch(
        NormalizedBatch(project, tuple(sp for run in runs for sp in run))
    )
    unpriced = await svc.list_unpriced_models(ADMIN)
    assert unpriced
    assert all(m.calls > 0 for m in unpriced)
    model = unpriced[0].model
    await svc.save_pricing(ADMIN, [entry(model, "1")])
    start = "2026-10-01T00:00:00Z"
    updated = await svc.recompute_costs(ADMIN, project, start, "2026-11-15T00:00:00Z")
    assert updated > 0
    assert model not in {m.model for m in await svc.list_unpriced_models(ADMIN)}
    assert (
        await svc.recompute_costs(ADMIN, project, "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z")
        == 0
    )


async def test_redaction_rules_req_172(
    db: ContractDB, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc = admin(db, tmp_path)
    project = await svc.create_project(ADMIN, "P", "proj")
    defaults = await svc.get_redaction_rules(ADMIN, project.id)
    assert defaults.presets["secrets"] is True
    await svc.save_redaction_rules(ADMIN, project.id, RULES)
    assert await svc.get_redaction_rules(ADMIN, project.id) == RULES
    for bad, field in [
        (dto.PatternRule(name="broken", regex="("), "pattern_rules[0]"),
        (dto.PatternRule(name="slow", regex=r"(a+)+$"), "pattern_rules[0]"),
    ]:
        rules = RULES.model_copy(update={"pattern_rules": [bad]})
        with pytest.raises(ValidationError) as exc:
            await svc.save_redaction_rules(ADMIN, project.id, rules)
        assert exc.value.field == field
    caplog.set_level(logging.DEBUG)
    sample = "mail bob@example.com about TCK-1234"
    out = svc.preview_redaction(ADMIN, RULES, sample)
    assert "bob@example.com" not in out
    assert "TCK-1234" not in out
    assert "[REDACTED:ticket]" in out
    assert "bob@example.com" not in caplog.text
    assert await svc.get_redaction_rules(ADMIN, project.id) == RULES  # preview saved nothing


async def test_saved_filters_req_102a(db: ContractDB, tmp_path: Path) -> None:
    svc = admin(db, tmp_path)
    a = await svc.create_project(ADMIN, "A", "proj-a")
    b = await svc.create_project(ADMIN, "B", "proj-b")
    viewer = dto.Viewer("v", "V", is_admin=False, allowed_project_ids=frozenset({a.id}))
    saved = await svc.save_filter(
        viewer, a.id, "errors", "?status=error&model=gpt-4o&sort=cost_desc"
    )
    assert saved.query == "status=error&model=gpt-4o&sort=cost_desc"
    with pytest.raises(ValidationError) as exc:
        await svc.save_filter(viewer, a.id, "bad", "status=nope")
    assert exc.value.field == "status"
    renamed = await svc.save_filter(viewer, a.id, "errors (gpt)", "status=error", saved.id)
    assert renamed.id == saved.id
    assert [f.name for f in await svc.list_saved_filters(viewer, a.id)] == ["errors (gpt)"]
    with pytest.raises(Forbidden):
        await svc.save_filter(viewer, b.id, "x", "status=ok")
    with pytest.raises(Forbidden):
        await svc.list_saved_filters(viewer, b.id)
    with pytest.raises(NotFound):
        await svc.delete_filter(ADMIN, b.id, saved.id)  # a filter of another Project
    await svc.delete_filter(viewer, a.id, saved.id)
    assert await svc.list_saved_filters(viewer, a.id) == []


async def test_demo_mode_rejects_every_mutation_req_231b(db: ContractDB, tmp_path: Path) -> None:
    project = await admin(db, tmp_path).create_project(ADMIN, "P", "proj")
    demo = admin(db, tmp_path, **{"demo.enabled": True})
    mutations = [
        demo.create_project(ADMIN, "X", "x-proj"),
        demo.update_project_settings(ADMIN, project.id, dto.ProjectSettingsPatch(name="Y")),
        demo.delete_project(ADMIN, project.id, "P"),
        demo.create_api_key(ADMIN, project.id, "ci"),
        demo.save_pricing(ADMIN, [entry("m", "1")]),
        demo.import_pricing_csv(ADMIN, "x"),
        demo.save_redaction_rules(ADMIN, project.id, RULES),
        demo.recompute_costs(ADMIN, project.id, "now-1d"),
        demo.save_filter(USER, project.id, "f", "status=ok"),
        demo.delete_filter(USER, project.id, "01JB0000000000000000000000"),
    ]
    for call in mutations:
        with pytest.raises(Forbidden) as exc:
            await call
        assert exc.value.reason == "demo mode"
    assert await demo.list_projects_admin(ADMIN)  # reads still work
    assert "[REDACTED" in demo.preview_redaction(ADMIN, RULES, "TCK-1")


def event(project: str, n: int, kind: str = "spans_committed") -> ChangeEvent:
    keys = tuple(RunKey(project, (i + 1).to_bytes(16, "big")) for i in range(n))
    return ChangeEvent(kind, project, keys, False, BASE_NS + NS)  # type: ignore[arg-type]


async def test_subscribe_coalesces_and_filters_req_140b(db: ContractDB) -> None:
    a = await db.make_project("sub-a")
    b = await db.make_project("sub-b")
    q = QueryService(db.require_backend(), b"s" * 32, clock_ns=db.clock)
    viewer = dto.Viewer("v", "V", is_admin=False, allowed_project_ids=frozenset({a}))
    feed = db.require_backend().changes
    notices: list[dto.ChangeNotice] = []
    started = asyncio.Event()

    async def consume() -> None:
        stream = q.subscribe(viewer, min_interval=2.0)
        task = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.05)
        started.set()
        notices.append(await task)
        notices.append(await anext(stream))
        await stream.aclose()

    consumer = asyncio.create_task(consume())
    await started.wait()
    feed.publish_nowait(event(a, 1))
    await asyncio.sleep(0.02)
    for _ in range(1000):  # burst inside one 2 s interval → one notice (slow runners too)
        feed.publish_nowait(event(a, 3))
        feed.publish_nowait(event(b, 2))  # another Project: never delivered
    await asyncio.sleep(0.01)
    feed.publish_nowait(event(a, 300, "runs_finalized"))
    await asyncio.wait_for(consumer, 10)
    first, second = notices
    assert first.project_ids == [a]
    assert len(first.run_keys) == 1
    assert second.project_ids == [a]
    assert {k.project_id for k in second.run_keys} == {a}
    assert len(second.run_keys) == 200
    assert second.truncated
    assert set(second.kinds) <= {"spans_committed", "runs_finalized"}
    with pytest.raises(ValidationError):
        await anext(q.subscribe(viewer, min_interval=0))
