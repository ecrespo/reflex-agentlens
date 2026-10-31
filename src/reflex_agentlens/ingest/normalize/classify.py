"""Span category from the mapping rules (plan task P3-02.2; Tech Design §5.3).

Thin adapter over :func:`reflex_agentlens.core.semconv.mapping.classify`, so the normalizer
depends on one ingest-level function; the rules themselves live in the declarative table.
"""

from __future__ import annotations

from reflex_agentlens.core.semconv.mapping import Classification, classify
from reflex_agentlens.ingest.normalize.spans import SpanDraft

__all__ = ["Classification", "classify_draft"]


def classify_draft(d: SpanDraft) -> Classification:
    return classify(d.attributes)
