"""ToolCall derivation (plan task P3-03.2; REQ-033.b, Data Model §2.12).

Arguments and results stay in the span attributes and are read at query time through the
mapping (Data Model N1), so only metadata is derived here.
"""

from __future__ import annotations

from reflex_agentlens.core.semconv.mapping import Classification, resolve
from reflex_agentlens.ingest.normalize.common import content_dropped, outcome, text
from reflex_agentlens.ingest.normalize.spans import SpanDraft
from reflex_agentlens.store.records import ToolCallRow

__all__ = ["tool_call"]


def tool_call(d: SpanDraft, cls: Classification) -> ToolCallRow | None:
    if cls.category != "tool":
        return None
    status, error_type = outcome(d)
    return ToolCallRow(
        start_ns=d.start_ns,
        duration_ns=d.end_ns - d.start_ns,
        status=status,
        tool_name=text(resolve(d.attributes, "tool_name"), 256) or "unknown",
        tool_call_id=text(resolve(d.attributes, "tool_call_id"), 256),
        tool_type=text(resolve(d.attributes, "tool_type"), 32),
        error_type=error_type,
        content_dropped=content_dropped(d),
    )
