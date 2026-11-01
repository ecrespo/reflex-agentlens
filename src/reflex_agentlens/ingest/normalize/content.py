"""Content drop for Projects with content storage off (plan task P3-04.2; REQ-040.a).

Removes the content-bearing attributes (Tech Design §5.3, ``CONTENT_KEYS`` and
``CONTENT_KEY_PREFIXES``) and the GenAI message events, records what was removed in
``dropped_keys`` (attribute keys, and ``event:<name>`` for events) and sets the
``content_dropped`` flag. Metadata (models, tokens, timings, tool names, status) is kept.
"""

from __future__ import annotations

import dataclasses

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.ingest.normalize.spans import FLAG_CONTENT_DROPPED, SpanDraft
from reflex_agentlens.store.attrs import JsonValue

__all__ = ["drop_content", "is_content_key"]


def is_content_key(key: str) -> bool:
    return key in k.CONTENT_KEYS or key.startswith(k.CONTENT_KEY_PREFIXES)


def _strip(attrs: dict[str, JsonValue], dropped: set[str]) -> dict[str, JsonValue]:
    kept: dict[str, JsonValue] = {}
    for key, value in attrs.items():
        if is_content_key(key):
            dropped.add(key)
        else:
            kept[key] = value
    return kept


def drop_content(d: SpanDraft) -> SpanDraft:
    dropped: set[str] = set()
    events = []
    for ev in d.events:
        name = ev.get("name")
        if isinstance(name, str) and name in k.MESSAGE_EVENTS:
            dropped.add(f"event:{name}")
            continue
        attrs = ev.get("attributes")
        events.append(
            {**ev, "attributes": _strip(attrs, dropped)} if isinstance(attrs, dict) else ev
        )
    attributes = _strip(d.attributes, dropped)
    if not dropped:
        return d
    return dataclasses.replace(
        d,
        attributes=attributes,
        events=events,
        dropped_keys=sorted(set(d.dropped_keys) | dropped),
        flags=d.flags | FLAG_CONTENT_DROPPED,
    )
