"""Which dialects a span speaks, and where they disagree (plan task P3-01.2; REQ-035.b, DD-009).

Resolution itself is the priority order of :data:`.mapping.FIELDS` (spec sources first, so the
spec value wins). This module reports the dialects present and the fields whose sources disagree,
for golden tests and the trace page's raw view; it never changes a resolved value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.core.semconv.mapping import (
    FIELDS,
    Attr,
    Dialect,
    Json,
    Mapped,
    Prefixed,
    Source,
)

__all__ = ["conflicts", "detect", "source_keys", "table_markdown"]

_PREFIXES: tuple[tuple[str, Dialect], ...] = (
    ("gen_ai.", "spec"),
    ("llm.", "openinference"),
    ("openinference.", "openinference"),
    ("langsmith.", "langsmith"),
    ("pydantic_ai.", "pydantic_ai"),
    ("reflex_agentkit.", "reflex"),
    ("reflex_mcphost.", "reflex"),
)
_DEPRECATED = frozenset(
    s.key
    for sources in FIELDS.values()
    for s in sources
    if isinstance(s, Attr) and s.dialect == "deprecated"
)


def _keys(source: Source) -> tuple[str, ...]:
    return source.keys if isinstance(source, Prefixed) else (source.key,)


def detect(attrs: Mapping[str, Any]) -> frozenset[Dialect]:
    """Dialects with at least one attribute on the span."""
    found: set[Dialect] = set()
    for key in attrs:
        if key in _DEPRECATED:
            found.add("deprecated")
            continue
        for prefix, dialect in _PREFIXES:
            if key.startswith(prefix):
                found.add(dialect)
                break
        else:
            if key in (k.PAI_AGENT_NAME, k.PAI_MODEL_REQUEST_PARAMETERS, k.PAI_FINAL_RESULT):
                found.add("pydantic_ai")
            elif key in (k.OI_SESSION_ID, k.OI_TOOL_NAME, k.OI_INPUT_VALUE, k.OI_OUTPUT_VALUE):
                found.add("openinference")
    return frozenset(found)


def conflicts(attrs: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
    """Fields with two or more present sources holding different values, in priority order."""
    out: dict[str, tuple[Any, ...]] = {}
    for name, sources in FIELDS.items():
        values = []
        for source in sources:
            value = source.get(attrs)
            if value is not None and value != "" and value not in values:
                values.append(value)
        if len(values) > 1:
            out[name] = tuple(values)
    return out


def source_keys(name: str) -> tuple[str, ...]:
    """Every attribute key read for field ``name``, in priority order (docs, P9-01)."""
    return tuple(key for source in FIELDS[name] for key in _keys(source))


def _describe(source: Source) -> str:
    if isinstance(source, Json):
        path = "$" + "".join(f".{p}" for p in source.path)
        return f"`json({source.key}, {path})`"
    if isinstance(source, Mapped):
        return f"`{source.key}` (mapped)"
    if isinstance(source, Prefixed):
        return " / ".join(f"`{key}`" for key in source.keys)
    when = f" (when `{source.when[0]}={source.when[1]}`)" if source.when else ""
    return f"`{source.key}`{when}"


def table_markdown() -> str:
    """The mapping table as Markdown, generated from code (docs, plan task P9-01)."""
    lines = ["| Field | Sources (priority) |", "|---|---|"]
    for name, sources in FIELDS.items():
        lines.append(f"| {name} | {' · '.join(_describe(s) for s in sources)} |")
    return "\n".join(lines) + "\n"
