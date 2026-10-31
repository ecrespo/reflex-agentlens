"""Pricing, redaction, ingest settings and saved filter contract tests (plan task P2-08.2).

REQ-060.a/c (immutable, time-resolved pricing versions), REQ-102.a (saved filters),
REQ-171.b (unpriced models), REQ-172 / REQ-006.c (rule sets bump ``settings_version``).
"""

from __future__ import annotations

import pytest

from reflex_agentlens.core.errors import Conflict, NotFound, ValidationError
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.store.records import (
    NewSavedFilter,
    PricingEntry,
    RedactionRuleSet,
)
from tests.contract.conftest import NOW_NS, ContractDB
from tests.contract.dataset import dataset
from tests.contract.test_reads import load

GPT = PricingEntry("gpt-4o", "exact", 2_500_000_000, 10_000_000_000, "openai", 1_250_000_000)
CLAUDE = PricingEntry("claude-*", "glob", 3_000_000_000, 15_000_000_000)


async def test_pricing_versions_are_immutable_and_resolved_by_time(db: ContractDB) -> None:
    """REQ-060.a: saving creates a new version; REQ-060.c: lookup by effective time."""
    backend = db.require_backend()
    with pytest.raises(NotFound):
        await backend.get_pricing()
    v1 = await backend.save_pricing(
        [GPT], "bootstrap", effective_from_ns=0, source="bundled", note="seed"
    )
    assert (v1.meta.version, v1.meta.effective_from_ns, v1.meta.source) == (1, 0, "bundled")
    assert v1.entries == (GPT,)

    cheaper = PricingEntry("gpt-4o", "exact", 2_000_000_000, 8_000_000_000, "openai")
    v2 = await backend.save_pricing([cheaper, CLAUDE], "ana", effective_from_ns=NOW_NS + 100)
    assert (v2.meta.version, v2.meta.created_by, v2.meta.source) == (2, "ana", "editor")
    assert v2.entries == (CLAUDE, cheaper)  # ordered by provider, then pattern

    assert (await backend.get_pricing(NOW_NS + 99)).meta.version == 1
    assert (await backend.get_pricing(NOW_NS + 100)).meta.version == 2
    assert (await backend.get_pricing()).meta.version == 1  # "now" is before v2 takes effect
    db.clock.now_ns = NOW_NS + 200
    assert (await backend.get_pricing()).meta.version == 2
    v3 = await backend.save_pricing([], "ana")  # effective now by default
    assert v3.meta.effective_from_ns == NOW_NS + 200

    assert (await backend.get_pricing(0)).entries == (GPT,)  # v1 untouched by later saves
    metas = await backend.list_pricing_versions()
    assert [m.version for m in metas] == [1, 2, 3]
    assert metas[0] == v1.meta


@pytest.mark.parametrize(
    "entries",
    [
        [PricingEntry("gpt*", "exact", 1, 1)],
        [PricingEntry("gpt", "glob", 1, 1)],
        [PricingEntry("gpt", "exact", -1, 1)],
        [PricingEntry("gpt", "exact", 1, 1, cached_input_nusd_per_mtok=-5)],
        [PricingEntry("", "exact", 1, 1)],
        [PricingEntry("gpt", "exact", 1, 1), PricingEntry("gpt", "exact", 2, 2)],
    ],
)
async def test_invalid_pricing_is_rejected(db: ContractDB, entries: list[PricingEntry]) -> None:
    backend = db.require_backend()
    with pytest.raises(ValidationError):
        await backend.save_pricing(entries, "ana")
    assert await backend.list_pricing_versions() == []


async def test_list_unpriced_models(db: ContractDB) -> None:
    """REQ-171.b: the unpriced model of the dataset, with its call count and last use."""
    runs = dataset(4, 600)
    project = await load(db, runs)
    calls = [sp.llm_call for run in runs for sp in run if sp.llm_call and sp.llm_call.unpriced]
    got = await db.require_backend().list_unpriced_models(frozenset({project}))
    assert [(u.provider, u.model, u.calls, u.last_seen_ns) for u in got] == [
        ("ollama", "llama3.3:70b", len(calls), max(c.start_ns for c in calls))
    ]
    assert await db.require_backend().list_unpriced_models(frozenset()) == []


async def test_redaction_rules_and_ingest_settings(db: ContractDB) -> None:
    """REQ-172: rules are stored per Project and saving bumps ``settings_version``."""
    backend = db.require_backend()
    project = await db.make_project()
    first = await backend.get_ingest_settings(project)
    assert first.redaction.presets == {
        "secrets": True, "email": False, "phone": False, "credit_card": False,
    }  # fmt: skip
    assert (first.redaction.pattern_rules, first.redaction.field_rules) == ([], [])
    assert (first.content_storage, first.span_rate) == (True, 20000)

    rules = RedactionRuleSet(
        presets={"secrets": True, "email": True},
        pattern_rules=[{"name": "ticket", "regex": "TCK-[0-9]+", "enabled": True}],
        field_rules=[{"key_glob": "user.*", "action": "mask"}],
        updated_by="ana",
    )
    db.clock.now_ns += 7
    await backend.save_redaction_rules(project, rules)
    after = await backend.get_ingest_settings(project)
    assert after.settings_version == first.settings_version + 1
    assert after.redaction.presets == rules.presets
    assert after.redaction.pattern_rules == rules.pattern_rules
    assert after.redaction.field_rules == rules.field_rules
    assert (after.redaction.updated_at_ns, after.redaction.updated_by) == (NOW_NS + 7, "ana")

    too_many = RedactionRuleSet({}, [{"name": str(i)} for i in range(51)], [])
    with pytest.raises(ValidationError):
        await backend.save_redaction_rules(project, too_many)
    with pytest.raises(NotFound):
        await backend.get_ingest_settings("01NOPROJECT")
    with pytest.raises(NotFound):
        await backend.save_redaction_rules("01NOPROJECT", rules)


async def test_saved_filters(db: ContractDB) -> None:
    """REQ-102.a: per-Project named filters; names unique per Project; update by id."""
    backend = db.require_backend()
    a, b = await db.make_project(), await db.make_project()
    fid = new_ulid()
    saved = await backend.save_filter(
        NewSavedFilter(fid, a, "errors", "status=error", created_by="ana")
    )
    assert (saved.id, saved.project_id, saved.name, saved.query) == (
        fid,
        a,
        "errors",
        "status=error",
    )
    assert saved.created_at_ns == saved.updated_at_ns == NOW_NS
    other = await backend.save_filter(NewSavedFilter(new_ulid(), a, "costly", "cost_min=1"))
    await backend.save_filter(
        NewSavedFilter(new_ulid(), b, "errors", "status=error")
    )  # other Project
    assert [f.name for f in await backend.list_saved_filters(a)] == ["costly", "errors"]

    with pytest.raises(Conflict):
        await backend.save_filter(NewSavedFilter(new_ulid(), a, "errors", "x=1"))
    with pytest.raises(Conflict):  # renaming onto an existing name
        await backend.save_filter(NewSavedFilter(other.id, a, "errors", "x=1"))
    with pytest.raises(Conflict):  # an id that belongs to another Project
        await backend.save_filter(NewSavedFilter(fid, b, "moved", "x=1"))

    db.clock.now_ns += 3
    updated = await backend.save_filter(NewSavedFilter(fid, a, "all errors", "status=error&x=1"))
    assert (updated.name, updated.query) == ("all errors", "status=error&x=1")
    assert (updated.created_at_ns, updated.updated_at_ns) == (NOW_NS, NOW_NS + 3)
    assert updated.created_by == "ana"

    await backend.delete_filter(fid)
    assert [f.name for f in await backend.list_saved_filters(a)] == ["costly"]
    with pytest.raises(NotFound):
        await backend.delete_filter(fid)
    with pytest.raises(ValidationError):
        await backend.save_filter(NewSavedFilter(new_ulid(), a, " ", "x"))
    with pytest.raises(ValidationError):
        await backend.save_filter(NewSavedFilter(new_ulid(), a, "long", "x" * 4097))
    with pytest.raises(NotFound):
        await backend.list_saved_filters("01NOPROJECT")
