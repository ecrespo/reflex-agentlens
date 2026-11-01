"""Order invariance and idempotency through the whole ingest path (plan task P3-12.1).

OTLP requests → ``normalize_batch`` → ``write_group``, with the spans permuted, split into
batches, grouped differently and partly duplicated, must give the same derived views (Runs,
Sessions, LLM calls, tool calls, approvals, graph nodes, Sources) as one ordered write.
REQ-037.a/b, REQ-038.a, REQ-365; on SQLite and PostgreSQL.
"""

from __future__ import annotations

import itertools
import random
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from reflex_agentlens.ingest.cost import PricingSnapshot
from reflex_agentlens.ingest.normalize.pipeline import BatchContext, normalize_batch
from reflex_agentlens.ingest.redaction.rules import compile_rules
from reflex_agentlens.store.records import (
    NormalizedBatch,
    PricingEntry,
    RedactionRuleSet,
    WriteResult,
)
from tests.contract.conftest import ContractDB
from tests.contract.snapshot import snapshot
from tests.unit.ingest import otlp

GPT = PricingEntry("gpt-4o*", "glob", 2_500_000_000, 10_000_000_000)


async def _pricing(db: ContractDB) -> PricingSnapshot:
    """A version stored in the database (``llm_calls.pricing_version`` is a foreign key)."""
    backend = db.require_backend()
    if not await backend.list_pricing_versions():
        await backend.save_pricing([GPT], "test", effective_from_ns=0)
    return PricingSnapshot([await backend.get_pricing(0)])


def _agent_trace(trace: int, conversation: str, base_ago: int) -> list[Span]:
    """An agent run: root → chat (LLM) → tool → chat, with a session ID on the root."""
    t, b = base_ago, trace * 10  # span IDs unique across traces (the snapshot sorts by span_id)
    return [
        otlp.span(
            trace, b + 1, None, t + 10, t, name="invoke_agent support",
            gen_ai__operation__name="invoke_agent", gen_ai__agent__name="support",
            gen_ai__conversation__id=conversation,
        ),
        otlp.span(
            trace, b + 2, b + 1, t + 9, t + 7, name="chat gpt-4o",
            gen_ai__operation__name="chat", gen_ai__provider__name="openai",
            gen_ai__request__model="gpt-4o", gen_ai__usage__input_tokens=100 + trace,
            gen_ai__usage__output_tokens=20,
        ),
        otlp.span(
            trace, b + 3, b + 1, t + 6, t + 5, name="execute_tool lookup",
            gen_ai__operation__name="execute_tool", gen_ai__tool__name="lookup",
            gen_ai__tool__call__id=f"call-{trace}",
        ),
        otlp.span(
            trace, b + 4, b + 1, t + 4, t + 2, name="chat gpt-4o",
            gen_ai__operation__name="chat", gen_ai__provider__name="openai",
            gen_ai__request__model="gpt-4o", gen_ai__usage__input_tokens=200,
            gen_ai__usage__output_tokens=30 + trace,
        ),
    ]  # fmt: skip


SPANS: tuple[Span, ...] = tuple(
    sp
    for trace in range(1, 7)
    for sp in _agent_trace(trace, f"conv-{trace % 3}", base_ago=20 * trace)
)


def _ctx(project_id: str, pricing: PricingSnapshot) -> BatchContext:
    return BatchContext(
        project_id=project_id,
        received_at_ns=otlp.NOW_NS,
        rules=compile_rules(RedactionRuleSet({"secrets": True}, [], [])),
        session_secret=b"s" * 32,
        pricing=pricing,
    )


def _batch(project_id: str, spans: list[Span], pricing: PricingSnapshot) -> NormalizedBatch:
    result = normalize_batch(otlp.request(*spans), _ctx(project_id, pricing))
    assert result.rejections.total == 0
    return result.batch


async def _snap(db: ContractDB, project_id: str) -> dict[str, Any]:
    async with db.engine.connect() as conn:
        return await conn.run_sync(snapshot, project_id)


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    seed=st.integers(0, 2**32 - 1),
    cuts=st.lists(st.integers(1, len(SPANS) - 1), max_size=8),
    group_size=st.integers(1, 4),
    duplicates=st.lists(st.integers(0, len(SPANS) - 1), max_size=6),
)
async def test_any_order_split_grouping_and_duplicates_give_identical_views(
    db: ContractDB, seed: int, cuts: list[int], group_size: int, duplicates: list[int]
) -> None:
    """REQ-037.a (order and batching), REQ-038.a (duplicates ignored), REQ-365."""
    backend = db.require_backend()
    pricing = await _pricing(db)
    reference = await db.make_project()
    (ref,) = await backend.write_group([_batch(reference, list(SPANS), pricing)])
    assert isinstance(ref, WriteResult)

    other = await db.make_project()
    rng = random.Random(seed)  # noqa: S311 - test permutation
    shuffled = list(SPANS) + [SPANS[i] for i in duplicates]
    rng.shuffle(shuffled)
    bounds = [0, *sorted({c for c in cuts if c < len(shuffled)}), len(shuffled)]
    batches = [
        _batch(other, shuffled[lo:hi], pricing) for lo, hi in itertools.pairwise(bounds) if lo < hi
    ]
    for i in range(0, len(batches), group_size):
        outcomes = await backend.write_group(batches[i : i + group_size])
        assert all(isinstance(o, WriteResult) for o in outcomes)

    assert await _snap(db, other) == await _snap(db, reference)


async def test_reingesting_everything_changes_nothing(db: ContractDB) -> None:
    """REQ-037.b / REQ-038.a: a full re-export is a no-op for the derived views."""
    backend = db.require_backend()
    pricing = await _pricing(db)
    project = await db.make_project()
    (first,) = await backend.write_group([_batch(project, list(SPANS), pricing)])
    assert isinstance(first, WriteResult)
    assert first.inserted == len(SPANS)
    before = await _snap(db, project)
    (again,) = await backend.write_group([_batch(project, list(reversed(SPANS)), pricing)])
    assert isinstance(again, WriteResult)
    assert (again.inserted, again.duplicates) == (0, len(SPANS))
    assert await _snap(db, project) == before
    assert len(before["llm_calls"]) == 12
    assert len(before["tool_calls"]) == 6
