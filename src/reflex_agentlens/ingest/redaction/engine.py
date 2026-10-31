"""Apply compiled redaction rules to a span draft (plan task P3-04.1; REQ-039.a/b/d, DD-013).

Order: field rules (``drop`` removes the key, ``mask`` replaces the value), then pattern rules on
every string leaf of span, event, link and resource attributes and the status message. Exception
messages and stack traces are event attributes, so they are covered. A string that holds JSON
(``gen_ai.input.messages`` and friends) is parsed, redacted leaf by leaf and re-serialized, so a
match never breaks the document. A pattern that exceeds its budget replaces the **whole** value
with ``[REDACTED:timeout]`` (fail closed). The result carries the ``redacted`` and
``redaction_timeout`` span flags (Data Model §2.10).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from reflex_agentlens.ingest.normalize.spans import (
    FLAG_REDACTED,
    FLAG_REDACTION_TIMEOUT,
    SpanDraft,
    map_strings,
)
from reflex_agentlens.ingest.redaction.rules import TIMEOUT_S, CompiledRules, PatternRule
from reflex_agentlens.store.attrs import JsonValue

__all__ = ["FIELD_MARKER", "TIMEOUT_MARKER", "redact_draft", "redact_text"]

TIMEOUT_MARKER = "[REDACTED:timeout]"
FIELD_MARKER = "[REDACTED:field]"


class _Outcome:
    __slots__ = ("changed", "timeouts")

    def __init__(self) -> None:
        self.changed = False
        self.timeouts = 0


def _apply(rule: PatternRule, text: str) -> str:
    if rule.validator is None:
        return str(rule.pattern.sub(rule.marker, text, timeout=TIMEOUT_S))
    check = rule.validator
    return str(
        rule.pattern.sub(
            lambda m: rule.marker if check(m.group(0)) else m.group(0), text, timeout=TIMEOUT_S
        )
    )


def _plain(text: str, rules: CompiledRules, out: _Outcome) -> str:
    for rule in rules.patterns:
        try:
            text = _apply(rule, text)
        except TimeoutError:
            out.timeouts += 1
            return TIMEOUT_MARKER
    return text


def _leaves(value: Any, rules: CompiledRules, out: _Outcome) -> Any:
    if isinstance(value, str):
        return _plain(value, rules, out)
    if isinstance(value, list):
        return [_leaves(v, rules, out) for v in value]
    if isinstance(value, dict):
        return {key: _leaves(v, rules, out) for key, v in value.items()}
    return value


def _redact(text: str, rules: CompiledRules, out: _Outcome) -> str:
    stripped = text.lstrip()
    if stripped[:1] in ("[", "{"):
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        if isinstance(doc, (list, dict)):
            inner = _Outcome()
            new = _leaves(doc, rules, inner)
            out.timeouts += inner.timeouts
            return json.dumps(new, ensure_ascii=False) if new != doc else text
    return _plain(text, rules, out)


def redact_text(text: str, rules: CompiledRules) -> str:
    """One value, as ingest would store it (also used by the Settings preview)."""
    return _redact(text, rules, _Outcome())


def _fields(
    attrs: dict[str, JsonValue], rules: CompiledRules, out: _Outcome
) -> dict[str, JsonValue]:
    if not rules.fields:
        return attrs
    kept: dict[str, JsonValue] = {}
    for key, value in attrs.items():
        rule = next((r for r in rules.fields if r.pattern.fullmatch(key)), None)
        if rule is None:
            kept[key] = value
        else:
            out.changed = True
            if rule.action == "mask":
                kept[key] = FIELD_MARKER
    return kept


def _on_attrs(container: dict[str, JsonValue], rules: CompiledRules, out: _Outcome) -> Any:
    attrs = container.get("attributes")
    return (
        {**container, "attributes": _fields(attrs, rules, out)}
        if isinstance(attrs, dict)
        else container
    )


def redact_draft(d: SpanDraft, rules: CompiledRules) -> SpanDraft:
    """REQ-039.a: every string value of the span, before anything is written."""
    if rules.empty:
        return d
    out = _Outcome()
    staged = dataclasses.replace(
        d,
        attributes=_fields(d.attributes, rules, out),
        resource=_fields(d.resource, rules, out),
        events=[_on_attrs(ev, rules, out) for ev in d.events],
        links=[_on_attrs(ln, rules, out) for ln in d.links],
    )

    def one(text: str) -> str:
        new = _redact(text, rules, out)
        if new != text:
            out.changed = True
        return new

    result = map_strings(staged, one) if rules.patterns else staged
    flags = result.flags
    if out.changed:
        flags |= FLAG_REDACTED
    if out.timeouts:
        flags |= FLAG_REDACTION_TIMEOUT | FLAG_REDACTED
    return dataclasses.replace(result, flags=flags) if flags != d.flags else result
