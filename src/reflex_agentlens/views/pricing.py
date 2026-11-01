"""Pricing editor view model (plan task P6-10.2; REQ-060.c, 171).

Prices are edited as text and parsed into ``Decimal`` here, never through ``float``. A row's match
kind follows its pattern (``*`` makes it a glob), so the editor cannot produce the "glob without
``*``" mismatch the service rejects. :func:`entries_from_rows` returns every problem at once,
each naming its row and column; the service validates the result again when it saves a new
version (REQ-060.c: earlier versions never change).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from reflex_agentlens.query.dto import (
    PricingEntryView,
    PricingTable,
    PricingVersionInfo,
    UnpricedModel,
)
from reflex_agentlens.views.fmt import fmt_int, fmt_time

__all__ = [
    "COLUMNS",
    "MAX_PRICE",
    "PriceRow",
    "UnpricedRow",
    "csv_filename",
    "entries_from_rows",
    "price_rows",
    "unpriced_rows",
    "version_label",
]

MAX_PRICE = Decimal(1_000_000)  # USD per 1M tokens: far above any real price
MAX_PLACES = 9  # prices are stored in nano-USD per 1M tokens
COLUMNS: dict[str, str] = {
    "provider": "Provider",
    "pattern": "Model pattern",
    "input": "Input $/1M",
    "output": "Output $/1M",
    "cached": "Cached input $/1M",
    "cache_write": "Cache write $/1M",
}


class PriceRow(BaseModel):
    """One editable entry: every field is the text in its input."""

    model_config = ConfigDict(frozen=True)

    provider: str = ""
    pattern: str = ""
    input: str = ""
    output: str = ""
    cached: str = ""
    cache_write: str = ""


def _text(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def price_rows(table: PricingTable) -> list[PriceRow]:
    return [
        PriceRow(
            provider=e.provider,
            pattern=e.model_pattern,
            input=_text(e.input_usd_per_mtok),
            output=_text(e.output_usd_per_mtok),
            cached=_text(e.cached_input_usd_per_mtok),
            cache_write=_text(e.cache_write_usd_per_mtok),
        )
        for e in table.entries
    ]


def _price(
    text: str, label: str, where: str, errors: list[str], *, required: bool
) -> Decimal | None:
    text = text.strip()
    if not text:
        if required:
            errors.append(f"{where}: {label} is required.")
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite():
        errors.append(f"{where}: {label} must be a decimal number such as 2.50.")
        return None
    if value < 0:
        errors.append(f"{where}: {label} cannot be negative.")
        return None
    if value > MAX_PRICE:
        errors.append(f"{where}: {label} is above {fmt_int(int(MAX_PRICE))}.")
        return None
    if -value.normalize().as_tuple().exponent > MAX_PLACES:  # type: ignore[operator]
        errors.append(f"{where}: {label} has more than {MAX_PLACES} decimal places.")
        return None
    return value


def entries_from_rows(rows: Sequence[PriceRow]) -> tuple[list[PricingEntryView], list[str]]:
    """Rows → entries, or every validation message (REQ-171.a: non-negative decimals).
    Rows left completely empty are ignored."""
    entries: list[PricingEntryView] = []
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for i, row in enumerate(rows, start=1):
        if not any(v.strip() for v in row.model_dump().values()):
            continue
        where = f"Row {i}"
        pattern, provider = row.pattern.strip(), row.provider.strip()
        before = len(errors)
        if not pattern:
            errors.append(f"{where}: {COLUMNS['pattern']} is required.")
        elif len(pattern) > 200:
            errors.append(f"{where}: {COLUMNS['pattern']} is longer than 200 characters.")
        elif (provider, pattern) in seen:
            errors.append(f"{where}: same provider and pattern as row {seen[(provider, pattern)]}.")
        else:
            seen[(provider, pattern)] = i
        prices = {
            name: _price(getattr(row, name), COLUMNS[name], where, errors,
                         required=name in ("input", "output"))
            for name in ("input", "output", "cached", "cache_write")
        }  # fmt: skip
        if len(errors) > before:
            continue
        entries.append(
            PricingEntryView(
                provider=provider,
                model_pattern=pattern,
                match_kind="glob" if "*" in pattern else "exact",
                input_usd_per_mtok=prices["input"],  # type: ignore[arg-type]
                output_usd_per_mtok=prices["output"],  # type: ignore[arg-type]
                cached_input_usd_per_mtok=prices["cached"],
                cache_write_usd_per_mtok=prices["cache_write"],
            )
        )
    return entries, errors


def version_label(info: PricingVersionInfo) -> str:
    parts = [f"Version {info.version}", info.source, f"from {fmt_time(info.effective_from_ns)}"]
    if info.note:
        parts.append(info.note)
    return " · ".join(parts)


def csv_filename(version: int) -> str:
    return f"agentlens-pricing-v{version}.csv"


class UnpricedRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    calls: str
    last_seen: str


def unpriced_rows(models: Iterable[UnpricedModel]) -> list[UnpricedRow]:
    """REQ-171.b: most used first."""
    ordered = sorted(models, key=lambda m: (-m.calls, m.provider, m.model))
    return [
        UnpricedRow(provider=m.provider, model=m.model, calls=fmt_int(m.calls),
                    last_seen=fmt_time(m.last_seen_ns))
        for m in ordered
    ]  # fmt: skip
