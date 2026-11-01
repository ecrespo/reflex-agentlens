"""Shared chrome URL state: Project, Sources, time range, live (plan task P6-02.1; REQ-130.a).

API §7.2 query params ``project``, ``source`` (repeatable), ``from``, ``to``, ``live``. Decoding
never raises: an invalid value falls back to its default (a hand-edited URL must still render).
Encoding omits defaults so URLs stay short and stable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.query.dto import ProjectRef, SourceView, Viewer
from reflex_agentlens.query.timeexpr import check_time_expr

__all__ = [
    "CUSTOM",
    "DEFAULT_RANGE",
    "RANGES",
    "Chrome",
    "Option",
    "decode_chrome",
    "encode_chrome",
    "project_options",
    "source_options",
]

RANGES: dict[str, str] = {"1h": "now-1h", "24h": "now-24h", "7d": "now-7d", "30d": "now-30d"}
DEFAULT_RANGE = "24h"
CUSTOM = "custom"  # a from/to pair that is not one of RANGES
MAX_SOURCES = 32
MAX_ID = 64


@dataclass(frozen=True, slots=True)
class Chrome:
    project: str | None = None
    sources: tuple[str, ...] = ()
    range: str = DEFAULT_RANGE  # a RANGES key or "custom"
    from_: str = RANGES[DEFAULT_RANGE]
    to: str | None = None
    live: bool = True

    def with_range(self, key: str) -> Chrome:
        if key not in RANGES:
            return self
        return Chrome(self.project, self.sources, key, RANGES[key], None, self.live)


@dataclass(frozen=True, slots=True)
class Option:
    value: str
    label: str
    extra: dict[str, str] = field(default_factory=dict)


Params = Mapping[str, str | Sequence[str]]


def _first(params: Params, key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value[0] if value else None


def _all(params: Params, key: str) -> list[str]:
    value = params.get(key)
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _time(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return check_time_expr(value, "from")
    except ValidationError:
        return None


def decode_chrome(params: Params) -> Chrome:
    project = _first(params, "project")
    if project is not None and not (0 < len(project) <= MAX_ID and project.isprintable()):
        project = None
    sources: list[str] = []
    for label in _all(params, "source"):
        label = label.strip()
        if label and len(label) <= 256 and label.isprintable() and label not in sources:
            sources.append(label)
    from_ = _time(_first(params, "from"))
    to = _time(_first(params, "to"))
    if from_ is None:
        rng, from_, to = DEFAULT_RANGE, RANGES[DEFAULT_RANGE], None
    else:
        named = [k for k, v in RANGES.items() if v == from_]
        rng = named[0] if named and to is None else CUSTOM
    live = _first(params, "live") != "0"
    return Chrome(project, tuple(sources[:MAX_SOURCES]), rng, from_, to, live)


def encode_chrome(chrome: Chrome) -> list[tuple[str, str]]:
    """Query pairs in a stable order, defaults omitted."""
    pairs: list[tuple[str, str]] = []
    if chrome.project:
        pairs.append(("project", chrome.project))
    pairs.extend(("source", s) for s in chrome.sources)
    if not (chrome.range == DEFAULT_RANGE and chrome.to is None):
        pairs.append(("from", chrome.from_))
        if chrome.to is not None:
            pairs.append(("to", chrome.to))
    if not chrome.live:
        pairs.append(("live", "0"))
    return pairs


def project_options(viewer: Viewer, projects: Iterable[ProjectRef]) -> list[Option]:
    """The Project selector; a restricted viewer only ever sees its allowed Projects
    (REQ-191.c; ``query`` already filters, this is defence in depth)."""
    allowed = viewer.allowed_project_ids
    return [
        Option(p.id, p.name, {"slug": p.slug})
        for p in sorted(projects, key=lambda p: p.name.lower())
        if allowed is None or p.id in allowed
    ]


def source_options(sources: Iterable[SourceView]) -> list[Option]:
    seen: dict[str, Option] = {}
    for view in sources:
        label = view.source.label
        seen.setdefault(label, Option(label, label))
    return sorted(seen.values(), key=lambda o: o.label.lower())
