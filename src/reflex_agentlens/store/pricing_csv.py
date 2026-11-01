"""Pricing CSV (DD-016): parse the bundled seed and the editor's import, write the export.

Plan tasks P3-05.1 (parse, moved here from ``ingest.cost`` so ``query`` can use it) and P4-05.2
(export, REQ-171.c). Prices are decimal text → integer nano-USD per Mtok (Art. 5, no float).
"""

from __future__ import annotations

import csv
import dataclasses
import io
from collections.abc import Iterable

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.money import nusd_per_mtok_to_price, price_per_mtok_to_nusd
from reflex_agentlens.store.records import PricingEntry

__all__ = ["CSV_COLUMNS", "BundledPricing", "format_pricing_csv", "parse_pricing_csv"]

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


def _text(nusd: int | None) -> str:
    if nusd is None:
        return ""
    return format(nusd_per_mtok_to_price(nusd), "f")


def format_pricing_csv(entries: Iterable[PricingEntry], source: str = "", date: str = "") -> str:
    """The same columns as the seed, so an export can be imported again (REQ-171.c)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for e in entries:
        writer.writerow([
            e.provider, e.model_pattern,
            _text(e.input_nusd_per_mtok), _text(e.output_nusd_per_mtok),
            _text(e.cached_input_nusd_per_mtok), _text(e.cache_write_nusd_per_mtok),
            source, date,
        ])  # fmt: skip
    return out.getvalue()
