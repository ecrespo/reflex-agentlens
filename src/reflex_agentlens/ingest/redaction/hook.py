"""Python redaction hook (plan task P3-04.2; REQ-039.e, API §8 ``[ingest] redaction_hook``).

The hook is ``module:function``, loaded once at startup. It is called on each span **after**
rule-based redaction and before persistence with a plain ``dict`` view of the span::

    {"name": str, "status_message": str | None, "attributes": {...}, "resource": {...},
     "events": [{"name", "time_ns", "attributes"}], "links": [{..., "attributes"}]}

It may mutate and return that dict, return a new one with the same shape, or return ``None``
(no change). If it raises, or returns something that isn't a valid view, the span is rejected
with ``redaction_hook_error`` and never stored unredacted. Failures are reported by exception
class only, never with span content (REQ-323).
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import math
from collections.abc import Callable
from typing import Any

from reflex_agentlens.core.errors import ConfigError
from reflex_agentlens.ingest.normalize.spans import FLAG_REDACTED, SpanDraft

__all__ = ["HookFailed", "RedactionHook", "apply_view", "load_hook", "run_hook", "span_view"]

RedactionHook = Callable[[dict[str, Any]], "dict[str, Any] | None"]
_MAX_DEPTH = 64


class HookFailed(Exception):
    """The hook raised or returned an invalid view; ``reason`` is safe to log."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_KEY = "ingest.redaction_hook"


def load_hook(spec: str, source: str = "config") -> RedactionHook:
    """Resolve ``module:function``; ``ConfigError`` if it can't be imported or isn't callable."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ConfigError(_KEY, source, "must be 'module:function'")
    try:
        target: Any = importlib.import_module(module_name)
        for part in attr.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(_KEY, source, f"cannot load {spec!r} ({type(exc).__name__})") from None
    if not callable(target):
        raise ConfigError(_KEY, source, f"{spec!r} is not callable")
    return target  # type: ignore[no-any-return]


def span_view(d: SpanDraft) -> dict[str, Any]:
    """A deep copy, so a hook can't alter the draft behind the engine's back."""
    return copy.deepcopy(
        {
            "name": d.name,
            "status_message": d.status_message,
            "attributes": d.attributes,
            "resource": d.resource,
            "events": d.events,
            "links": d.links,
        }
    )


def _json(value: Any, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json(v, depth + 1) for key, v in value.items())
    return False


def _object(value: Any) -> bool:
    return isinstance(value, dict) and _json(value)


def _records(value: Any) -> bool:
    return isinstance(value, list) and all(
        _object(item) and _object(item.get("attributes", {})) for item in value
    )


def apply_view(d: SpanDraft, view: dict[str, Any]) -> SpanDraft:
    """Validate a view and copy it onto the draft; ``HookFailed`` if the shape is wrong."""
    name, status = view.get("name"), view.get("status_message")
    if not isinstance(name, str) or not (status is None or isinstance(status, str)):
        raise HookFailed("invalid name or status_message")
    if not _object(view.get("attributes")) or not _object(view.get("resource")):
        raise HookFailed("invalid attributes or resource")
    if not _records(view.get("events")) or not _records(view.get("links")):
        raise HookFailed("invalid events or links")
    return dataclasses.replace(
        d,
        name=name,
        status_message=status,
        attributes=view["attributes"],
        resource=view["resource"],
        events=view["events"],
        links=view["links"],
    )


def run_hook(d: SpanDraft, hook: RedactionHook) -> SpanDraft:
    """Call the hook on one span; sets the ``redacted`` flag when it changed anything."""
    view = span_view(d)
    try:
        result = hook(view)
    except Exception as exc:  # any hook failure rejects the span (fail closed)
        raise HookFailed(f"hook raised {type(exc).__name__}") from None
    if result is None:
        result = view
    if not isinstance(result, dict):
        raise HookFailed("hook returned a non-dict")
    out = apply_view(d, result)
    if span_view(out) == span_view(d):
        return d
    return dataclasses.replace(out, flags=out.flags | FLAG_REDACTED)
