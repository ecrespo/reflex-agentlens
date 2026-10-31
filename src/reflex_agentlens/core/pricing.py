"""Pricing entry resolution and per-call cost (REQ-061.a/b/c; Data Model §2.5 "Resolution").

Pure functions shared by ``ingest`` (cost at derivation time) and ``store`` (``recompute_costs``,
``rebuild``). Entries are matched structurally (:class:`PriceRule`), so this module does not
depend on ``store.records``.

Resolution for one call, normative order:

1. Try the response model, then the request model (REQ-061.b).
2. For each model, an ``exact`` entry wins over any ``glob`` entry.
3. Among exact entries, one for the call's provider beats one for any provider (``''``).
4. Among globs (``*`` is the only wildcard), the longest literal prefix (text before the first
   ``*``) wins; then provider-specific beats ``''``; then the longer pattern; then the pattern
   text, so the choice is deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Protocol, TypeVar

from reflex_agentlens.core.money import llm_call_cost_nusd

__all__ = ["PriceRule", "glob_matches", "llm_cost", "match_entry"]


class PriceRule(Protocol):
    @property
    def provider(self) -> str: ...
    @property
    def model_pattern(self) -> str: ...
    @property
    def match_kind(self) -> str: ...
    @property
    def input_nusd_per_mtok(self) -> int: ...
    @property
    def output_nusd_per_mtok(self) -> int: ...
    @property
    def cached_input_nusd_per_mtok(self) -> int | None: ...
    @property
    def cache_write_nusd_per_mtok(self) -> int | None: ...


R = TypeVar("R", bound=PriceRule)


@lru_cache(maxsize=4096)
def _glob_re(pattern: str) -> re.Pattern[str]:
    return re.compile("".join(".*" if part == "*" else re.escape(part)
                              for part in re.split(r"(\*)", pattern)))  # fmt: skip


def glob_matches(pattern: str, model: str) -> bool:
    """``*`` matches any run of characters; everything else is literal and case-sensitive."""
    return _glob_re(pattern).fullmatch(model) is not None


def _best_for(entries: Sequence[R], provider: str, model: str) -> R | None:
    usable = [e for e in entries if e.provider in ("", provider)]
    exact = [e for e in usable if e.match_kind == "exact" and e.model_pattern == model]
    if exact:
        return max(exact, key=lambda e: e.provider != "")
    globs = [e for e in usable if e.match_kind == "glob" and glob_matches(e.model_pattern, model)]
    if not globs:
        return None
    return max(
        globs,
        key=lambda e: (
            len(e.model_pattern.split("*", 1)[0]),
            e.provider != "",
            len(e.model_pattern),
            e.model_pattern,
        ),
    )


def match_entry(
    entries: Sequence[R],
    provider: str | None,
    response_model: str | None,
    request_model: str | None,
) -> R | None:
    """The entry pricing a call, or ``None`` (unpriced, REQ-061.c)."""
    prov = provider or ""
    models: Iterable[str | None] = (response_model, request_model)
    for model in models:
        if model:
            found = _best_for(entries, prov, model)
            if found is not None:
                return found
    return None


def llm_cost(
    entry: PriceRule | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> int | None:
    """Cost in nano-USD, or ``None`` when unpriced: no entry, or input/output tokens missing."""
    if entry is None or input_tokens is None or output_tokens is None:
        return None
    cached = min(cached_input_tokens or 0, input_tokens)
    return llm_call_cost_nusd(
        input_tokens,
        output_tokens,
        cached,
        p_in=entry.input_nusd_per_mtok,
        p_out=entry.output_nusd_per_mtok,
        p_cached=entry.cached_input_nusd_per_mtok,
        cache_write_tokens=cache_write_tokens or 0,
        p_cache_write=entry.cache_write_nusd_per_mtok,
    )
