"""Cost per LLMCall and the bundled pricing seed (plan tasks P3-05.1, P3-05.2).

REQ-060.a/b, REQ-061.a-c, DD-016, Data Model §2.5, Art. 5 (integer nano-USD only).

Pricing: the engine holds every pricing version in memory (refreshed on ``settings_changed``) and
prices a call with the version in effect at its start time, ``max(version)`` with
``effective_from_ns <= start`` (REQ-061.a), using :mod:`reflex_agentlens.core.pricing` for the
matching order (response model before request model, exact before glob). An unmatched model or
missing token counts leave the call unpriced: cost ``NULL``, ``unpriced = true`` (REQ-061.c).

Bundled seed: ``data/pricing.csv`` (generated at dev time by ``scripts/update_pricing.py``; no
runtime fetch, Art. 3) or ``[pricing] file`` is loaded as version 1 (``source="bundled"``,
effective from 0) when the database has no pricing version (Data Model §5, Analyze A-11).
"""

from __future__ import annotations

import csv
import dataclasses
import io
from collections.abc import Iterable, Sequence
from importlib import resources
from pathlib import Path
from typing import Protocol

from reflex_agentlens.core.errors import Conflict, ValidationError
from reflex_agentlens.core.money import price_per_mtok_to_nusd
from reflex_agentlens.core.pricing import llm_cost, match_entry
from reflex_agentlens.store.records import (
    LLMCallRow,
    PricingEntry,
    PricingSource,
    PricingVersion,
)

__all__ = [
    "CSV_COLUMNS",
    "BundledPricing",
    "PricingSnapshot",
    "bootstrap_pricing",
    "load_bundled_pricing",
    "parse_pricing_csv",
    "price_call",
]

CSV_COLUMNS = (
    "provider",
    "model_pattern",
    "input_per_mtok",
    "output_per_mtok",
    "cached_input_per_mtok",
    "cache_write_per_mtok",
    "source",
    "snapshot_date",
)


class PricingSnapshot:
    """Every pricing version, newest first; picks the one in effect at a timestamp."""

    def __init__(self, versions: Iterable[PricingVersion]) -> None:
        self.versions = tuple(sorted(versions, key=lambda v: v.meta.version, reverse=True))

    def at(self, t_ns: int) -> PricingVersion | None:
        for version in self.versions:
            if version.meta.effective_from_ns <= t_ns:
                return version
        return None


def price_call(row: LLMCallRow, snapshot: PricingSnapshot) -> LLMCallRow:
    """REQ-061: cost, ``unpriced`` and ``pricing_version`` for one derived call."""
    version = snapshot.at(row.start_ns)
    entry = None
    if version is not None:
        entry = match_entry(version.entries, row.provider, row.response_model, row.request_model)
    cost = llm_cost(
        entry,
        row.input_tokens,
        row.output_tokens,
        row.cached_input_tokens,
        row.cache_write_tokens,
    )
    return dataclasses.replace(
        row,
        cost_nusd=cost,
        unpriced=cost is None,
        pricing_version=None if cost is None or version is None else version.meta.version,
    )


# ── bundled seed (P3-05.1) ───────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True, slots=True)
class BundledPricing:
    entries: tuple[PricingEntry, ...]
    note: str  # snapshot date and upstream source, stored on version 1


def _price(raw: str, where: str) -> int:
    try:
        value = price_per_mtok_to_nusd(raw.strip())  # Decimal text → integer nano-USD, no float
    except (ValidationError, ArithmeticError, ValueError):
        raise ValidationError(where, "not a decimal price") from None
    if value < 0:
        raise ValidationError(where, "price cannot be negative")
    return value


def _optional_price(raw: str, where: str) -> int | None:
    return _price(raw, where) if raw.strip() else None


def parse_pricing_csv(text: str) -> BundledPricing:
    """Parse the DD-016 CSV; ``ValidationError`` names the offending line and column."""
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
        raise ValidationError("pricing.csv", f"header must be {','.join(CSV_COLUMNS)}")
    entries: list[PricingEntry] = []
    sources: set[str] = set()
    dates: set[str] = set()
    for row in reader:
        line = reader.line_num
        pattern = (row["model_pattern"] or "").strip()
        if not pattern:
            raise ValidationError(f"pricing.csv:{line}", "model_pattern is required")
        at = f"pricing.csv:{line}"
        entries.append(
            PricingEntry(
                model_pattern=pattern,
                match_kind="glob" if "*" in pattern else "exact",
                input_nusd_per_mtok=_price(row["input_per_mtok"] or "", f"{at}:input_per_mtok"),
                output_nusd_per_mtok=_price(row["output_per_mtok"] or "", f"{at}:output_per_mtok"),
                provider=(row["provider"] or "").strip(),
                cached_input_nusd_per_mtok=_optional_price(
                    row["cached_input_per_mtok"] or "", f"{at}:cached_input_per_mtok"
                ),
                cache_write_nusd_per_mtok=_optional_price(
                    row["cache_write_per_mtok"] or "", f"{at}:cache_write_per_mtok"
                ),
            )
        )
        sources.add((row["source"] or "").strip())
        dates.add((row["snapshot_date"] or "").strip())
    note = f"snapshot {', '.join(sorted(d for d in dates if d)) or 'unknown'}; source: " + (
        ", ".join(sorted(s for s in sources if s)) or "unknown"
    )
    return BundledPricing(tuple(entries), note[:500])


def load_bundled_pricing(path: Path | None = None) -> BundledPricing:
    """``[pricing] file`` when set, else the packaged ``data/pricing.csv`` (no network)."""
    if path is not None:
        return parse_pricing_csv(path.read_text(encoding="utf-8"))
    data = resources.files("reflex_agentlens.data").joinpath("pricing.csv")
    return parse_pricing_csv(data.read_text(encoding="utf-8"))


class _PricingStore(Protocol):
    async def list_pricing_versions(self) -> Sequence[object]: ...

    async def save_pricing(
        self,
        entries: Sequence[PricingEntry],
        author: str | None,
        *,
        effective_from_ns: int | None = None,
        source: PricingSource = "editor",
        note: str | None = None,
    ) -> PricingVersion: ...


async def bootstrap_pricing(store: _PricingStore, seed: BundledPricing) -> bool:
    """Save ``seed`` as version 1 on an empty database; ``True`` when this call created it.

    Idempotent across processes: if another process saved first, the ``Conflict`` on the
    version number is the expected outcome and nothing is written.
    """
    if await store.list_pricing_versions():
        return False
    try:
        await store.save_pricing(
            seed.entries, None, effective_from_ns=0, source="bundled", note=seed.note
        )
    except Conflict:
        return False
    return True
