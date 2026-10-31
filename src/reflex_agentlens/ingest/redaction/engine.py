"""Apply compiled redaction rules to a span draft (plan task P3-04.1; REQ-039.a/b/d, DD-013).

Order: field rules (``drop`` removes the key, ``mask`` replaces the value; matched on the dotted
path at any depth), then pattern rules on every string leaf of span, event, link and resource
attributes, the status message, the span name and event names. Integers and integral doubles
(attribute values and numbers inside JSON documents) are scanned as text, so a card number sent
as a number is caught.
Attribute *keys* are never rewritten (REQ-039.a covers values; keys drive the mapping). Exception
messages and stack traces are event attributes, so they are covered. A string that holds JSON
(``gen_ai.input.messages`` and friends) is parsed, redacted leaf by leaf and re-serialized, so a
match never breaks the document. A pattern that exceeds its budget replaces the **whole** value
with ``[REDACTED:timeout]`` (fail closed). The result carries the ``redacted`` and
``redaction_timeout`` span flags (Data Model §2.10).
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Callable
from typing import Any, TypeGuard

from reflex_agentlens.ingest.normalize.spans import (
    FLAG_REDACTED,
    FLAG_REDACTION_TIMEOUT,
    SpanDraft,
    map_strings,
)
from reflex_agentlens.ingest.redaction.rules import TIMEOUT_S, CompiledRules, PatternRule
from reflex_agentlens.store.attrs import B64_KEY, JsonValue

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


def _scannable(value: object) -> TypeGuard[float]:
    """Integers, and doubles holding an integer (a card number sent as ``4111….0``)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (
        isinstance(value, float) and math.isfinite(value) and value.is_integer()
    )


def _number(value: float, rules: CompiledRules, out: _Outcome) -> float | str:
    """A number can be a card or phone number: scan its digits, replace it if anything matched."""
    text = str(int(value))
    new = _plain(text, rules, out)
    return value if new == text else new


def _leaves(value: Any, rules: CompiledRules, out: _Outcome) -> Any:
    if isinstance(value, str):
        return _plain(value, rules, out)
    if _scannable(value):
        return _number(value, rules, out)
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


def _is_bytes(value: dict[str, JsonValue]) -> bool:
    return len(value) == 1 and isinstance(value.get(B64_KEY), str)


def _tree(value: JsonValue, path: str, rules: CompiledRules, out: _Outcome) -> JsonValue:
    """Non-string handling of one attribute value: nested field rules and integers."""
    if isinstance(value, dict) and not _is_bytes(value):
        return _fields(value, rules, out, f"{path}.")
    if isinstance(value, list):
        return [_tree(v, f"{path}.{i}", rules, out) for i, v in enumerate(value)]
    if rules.patterns and _scannable(value):
        new = _number(value, rules, out)
        if new != value:
            out.changed = True
        return new
    return value


def _fields(
    attrs: dict[str, JsonValue], rules: CompiledRules, out: _Outcome, prefix: str = ""
) -> dict[str, JsonValue]:
    """Field rules match the dotted path at any depth (``a.b`` for ``{"a": {"b": ...}}``,
    ``a.0.b`` inside arrays); ``drop`` removes the key, ``mask`` replaces the value."""
    kept: dict[str, JsonValue] = {}
    for key, value in attrs.items():
        path = f"{prefix}{key}"
        rule = next((r for r in rules.fields if r.matches(path)), None)
        if rule is None:
            kept[key] = _tree(value, path, rules, out)
            continue
        out.changed = True
        if rule.action == "mask":
            kept[key] = FIELD_MARKER
    return kept


def _on_attrs(container: dict[str, JsonValue], rules: CompiledRules, out: _Outcome) -> Any:
    attrs = container.get("attributes")
    if not isinstance(attrs, dict):
        return container
    return {**container, "attributes": _fields(attrs, rules, out)}


def _rename(event: dict[str, JsonValue], fn: Callable[[str], str]) -> dict[str, JsonValue]:
    name = event.get("name")
    return {**event, "name": fn(name)} if isinstance(name, str) else event


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

    result = staged
    if rules.patterns:
        mapped = map_strings(staged, one)
        events = [_rename(ev, one) for ev in mapped.events]
        result = dataclasses.replace(mapped, name=one(mapped.name), events=events)
    flags = result.flags
    if out.changed:
        flags |= FLAG_REDACTED
    if out.timeouts:
        flags |= FLAG_REDACTION_TIMEOUT | FLAG_REDACTED
    return dataclasses.replace(result, flags=flags) if flags != d.flags else result
