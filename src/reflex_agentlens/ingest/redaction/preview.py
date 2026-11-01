"""Redaction preview for Settings (plan task P3-04.2; REQ-172.b).

The only ``ingest`` module ``query`` may import. It runs the **unsaved** rule set through the
same engine ingest uses, so the preview equals what ingest would store (rules only: the hook,
truncation and content drop depend on server and Project settings). Nothing is persisted or
logged.

A sample is either plain text, or a span as JSON: an object with any of ``name``,
``status_message``, ``attributes``, ``resource``, ``events``, ``links``. A span sample gets the
field rules too, and is returned as JSON of the same keys.
"""

from __future__ import annotations

import json
from typing import Any

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.ingest.normalize.spans import SpanDraft
from reflex_agentlens.ingest.redaction.engine import redact_draft, redact_text
from reflex_agentlens.ingest.redaction.hook import HookFailed, apply_view, span_view
from reflex_agentlens.ingest.redaction.rules import validate_rule_set
from reflex_agentlens.store.records import RedactionRuleSet

__all__ = ["MAX_SAMPLE_BYTES", "preview_redaction"]

MAX_SAMPLE_BYTES = 64 * 1024
_SPAN_KEYS = ("name", "status_message", "attributes", "resource", "events", "links")
_EMPTY = SpanDraft(
    trace_id=bytes(16), span_id=bytes(8), parent_span_id=None, name="", otel_kind=0,
    start_ns=0, end_ns=0, status_code=0, status_message=None, attributes={}, events=[],
    links=[], resource={}, resource_schema_url=None, scope_name=None, scope_version=None,
    received_at_ns=0,
)  # fmt: skip


def _span_sample(sample: str) -> dict[str, Any] | None:
    if not sample.lstrip().startswith("{"):
        return None
    try:
        doc = json.loads(sample)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or not any(key in doc for key in _SPAN_KEYS):
        return None
    if any(key not in _SPAN_KEYS for key in doc):
        return None  # some other JSON document: redact it as text, leaf by leaf
    return doc


def preview_redaction(rules: RedactionRuleSet, sample: str) -> str:
    """``ValidationError`` for an invalid or slow rule set, or a sample over 64 KiB."""
    if len(sample.encode()) > MAX_SAMPLE_BYTES:
        raise ValidationError("sample", "at most 64 KiB")
    compiled = validate_rule_set(rules)
    doc = _span_sample(sample)
    if doc is None:
        return redact_text(sample, compiled)
    view = span_view(_EMPTY) | doc
    try:
        draft = apply_view(_EMPTY, view)
    except HookFailed as exc:
        raise ValidationError("sample", f"not a span: {exc.reason}") from None
    out = span_view(redact_draft(draft, compiled))
    return json.dumps({key: out[key] for key in doc}, ensure_ascii=False, indent=2)
