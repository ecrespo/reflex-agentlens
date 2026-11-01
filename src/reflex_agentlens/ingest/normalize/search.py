"""Search document per span (plan task P3-03.3; REQ-073.a, DD-006, Data Model §2.15).

GenAI spans: span name + message text parts + tool arguments and results + error and exception
messages. Non-GenAI spans: name and status message only. Built after redaction, truncation and
content drop, stored already normalized by ``core.search.search_normalize`` and cut to 32 KiB.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from reflex_agentlens.core.search import search_normalize, truncate_utf8
from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.core.semconv.mapping import Classification, resolve
from reflex_agentlens.core.semconv.messages import Conversation, extract
from reflex_agentlens.ingest.normalize.llm import events_of
from reflex_agentlens.ingest.normalize.spans import SpanDraft

__all__ = ["search_document"]


def _as_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return None


def _messages(conv: Conversation) -> Iterator[str | None]:
    for message in (*conv.input, *conv.output):
        for part in message.parts:
            if part.type in ("text", "reasoning"):
                yield part.content
            elif part.type == "tool_call":
                yield part.name
                yield _as_text(part.arguments)
            elif part.type == "tool_result":
                yield _as_text(part.result)


def _errors(d: SpanDraft) -> Iterator[str | None]:
    yield d.status_message
    for ev in d.events:
        attrs = ev.get("attributes")
        if ev.get("name") == k.EVENT_EXCEPTION and isinstance(attrs, dict):
            yield _as_text(attrs.get(k.EXCEPTION_TYPE))
            yield _as_text(attrs.get(k.EXCEPTION_MESSAGE))


def search_document(d: SpanDraft, cls: Classification) -> str | None:
    parts: list[str | None] = [d.name]
    if cls.is_genai:
        parts += _messages(extract(d.attributes, events_of(d)))
        parts += (_as_text(resolve(d.attributes, "tool_name")),)
        parts += (_as_text(resolve(d.attributes, "tool_args")),)
        parts += (_as_text(resolve(d.attributes, "tool_result")),)
        parts += _errors(d)
    else:
        parts.append(d.status_message)
    doc = search_normalize(" ".join(p for p in parts if p))
    return truncate_utf8(doc) or None
