"""Span re-derivation for ``rebuild`` (plan task P5-03.3; REQ-042.a, 035, 061)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from reflex_agentlens.core.semconv.mapping import MAPPING_VERSION
from reflex_agentlens.ingest.cost import PricingSnapshot
from reflex_agentlens.ingest.decode import decode_traces
from reflex_agentlens.ingest.normalize.pipeline import BatchContext, normalize_batch
from reflex_agentlens.ingest.redaction.rules import compile_rules
from reflex_agentlens.ingest.rederive import rederive
from reflex_agentlens.store.records import (
    NormalizedSpan,
    PricingEntry,
    PricingVersion,
    PricingVersionMeta,
    RedactionRuleSet,
)
from tests.unit.ingest import otlp

GOLDEN = sorted((Path(__file__).parents[2] / "golden" / "fixtures").glob("*.json"))
V1 = PricingVersion(
    PricingVersionMeta(1, 0, "bundled", None, 0, None),
    (
        PricingEntry("gpt-4o*", "glob", 2_500_000_000, 10_000_000_000),
        PricingEntry("claude-*", "glob", 3_000_000_000, 15_000_000_000),
    ),
)


def normalized(path: Path, pricing: PricingVersion = V1) -> list[NormalizedSpan]:
    ctx = BatchContext(
        project_id="01GOLDEN000000000000000000",
        received_at_ns=otlp.NOW_NS,
        rules=compile_rules(RedactionRuleSet({"secrets": True}, [], [])),
        session_secret=b"g" * 32,
        pricing=PricingSnapshot([pricing]),
    )
    spans: list[NormalizedSpan] = []
    for doc in json.loads(path.read_text())["requests"]:
        spans.extend(
            normalize_batch(decode_traces(json.dumps(doc).encode(), "json"), ctx).batch.spans
        )
    return spans


@pytest.mark.parametrize("path", GOLDEN, ids=lambda p: p.stem)
def test_rederiving_a_fresh_span_is_the_identity(path: Path) -> None:
    spans = normalized(path)
    assert spans
    for span in spans:
        assert rederive(span, V1) == span


def test_stale_derivations_are_replaced_and_raw_fields_kept() -> None:
    span = next(sp for sp in normalized(GOLDEN[0]) if sp.llm_call is not None or sp.tool_call)
    stale = dataclasses.replace(
        span, mapping_version=0, category="generic", llm_call=None, tool_call=None,
        search_doc=None, agent_name="old",
    )  # fmt: skip
    again = rederive(stale, V1)
    assert again == span
    assert again.mapping_version == MAPPING_VERSION
    assert (again.attributes, again.session, again.flags) == (
        span.attributes,
        span.session,
        span.flags,
    )


def test_current_pricing_applies_whatever_the_span_time_req_042a() -> None:
    priced = [sp for path in GOLDEN for sp in normalized(path) if sp.llm_call is not None]
    call = next(sp for sp in priced if not sp.llm_call.unpriced)  # type: ignore[union-attr]
    later = PricingVersion(
        PricingVersionMeta(2, call.start_ns + 10**15, "editor", None, 0, None),  # future-dated
        tuple(
            dataclasses.replace(e, input_nusd_per_mtok=e.input_nusd_per_mtok * 2)
            for e in V1.entries
        ),
    )
    again = rederive(call, later)
    assert again.llm_call is not None
    assert again.llm_call.pricing_version == 2
    assert again.llm_call.cost_nusd != call.llm_call.cost_nusd  # type: ignore[union-attr]
