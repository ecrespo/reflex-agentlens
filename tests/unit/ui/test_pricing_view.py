"""Pricing editor view model (plan task P6-10.2; REQ-060.c, 171)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from reflex_agentlens.query.dto import (
    PricingEntryView,
    PricingTable,
    PricingVersionInfo,
    UnpricedModel,
)
from reflex_agentlens.views.pricing import (
    PriceRow,
    csv_filename,
    entries_from_rows,
    price_rows,
    unpriced_rows,
    version_label,
)
from tests.unit.ui.factories import T0

INFO = PricingVersionInfo(version=3, effective_from_ns=T0, source="editor", note="cheaper",
                          created_at_ns=T0, created_by="ana")  # fmt: skip


def test_rows_round_trip_without_float_req_171a() -> None:
    table = PricingTable(
        info=INFO,
        entries=[
            PricingEntryView(provider="openai", model_pattern="gpt-4o", match_kind="exact",
                             input_usd_per_mtok=Decimal("2.5"),
                             output_usd_per_mtok=Decimal("10"),
                             cached_input_usd_per_mtok=Decimal("1.25")),
            PricingEntryView(model_pattern="claude-*", match_kind="glob",
                             input_usd_per_mtok=Decimal("0.000000001"),
                             output_usd_per_mtok=Decimal("15")),
        ],
    )  # fmt: skip
    rows = price_rows(table)
    assert rows[0] == PriceRow(provider="openai", pattern="gpt-4o", input="2.5", output="10",
                               cached="1.25")  # fmt: skip
    assert rows[1].input == "0.000000001"  # never 1E-9
    entries, errors = entries_from_rows(rows)
    assert errors == []
    assert entries == table.entries


def test_match_kind_follows_the_pattern() -> None:
    entries, _ = entries_from_rows(
        [PriceRow(pattern="gpt-*", input="1", output="2"), PriceRow(pattern="o1", input="1",
                                                                     output="2")]
    )  # fmt: skip
    assert [e.match_kind for e in entries] == ["glob", "exact"]


def test_blank_rows_are_ignored_and_fields_are_trimmed() -> None:
    entries, errors = entries_from_rows(
        [PriceRow(), PriceRow(provider=" openai ", pattern=" gpt ", input=" 1 ", output="2")]
    )
    assert errors == []
    assert (entries[0].provider, entries[0].model_pattern) == ("openai", "gpt")


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (PriceRow(input="1", output="2"), "Row 1: Model pattern is required."),
        (PriceRow(pattern="m", output="2"), "Row 1: Input $/1M is required."),
        (PriceRow(pattern="m", input="abc", output="2"),
         "Row 1: Input $/1M must be a decimal number such as 2.50."),
        (PriceRow(pattern="m", input="NaN", output="2"),
         "Row 1: Input $/1M must be a decimal number such as 2.50."),
        (PriceRow(pattern="m", input="1", output="-0.5"), "Row 1: Output $/1M cannot be negative."),
        (PriceRow(pattern="m", input="1", output="2", cached="2000000"),
         "Row 1: Cached input $/1M is above 1,000,000."),
        (PriceRow(pattern="m", input="0.0000000001", output="2"),
         "Row 1: Input $/1M has more than 9 decimal places."),
        (PriceRow(pattern="m" * 201, input="1", output="2"),
         "Row 1: Model pattern is longer than 200 characters."),
    ],
)  # fmt: skip
def test_validation_messages_name_row_and_column_req_171a(row: PriceRow, message: str) -> None:
    entries, errors = entries_from_rows([row])
    assert entries == []
    assert errors == [message]


def test_every_problem_is_reported_at_once() -> None:
    rows = [
        PriceRow(pattern="m", input="1", output="2"),
        PriceRow(pattern="m", input="x", output="-1"),
        PriceRow(pattern="m", input="1", output="2"),
    ]
    entries, errors = entries_from_rows(rows)
    assert len(entries) == 1
    assert errors == [
        "Row 2: same provider and pattern as row 1.",
        "Row 2: Input $/1M must be a decimal number such as 2.50.",
        "Row 2: Output $/1M cannot be negative.",
        "Row 3: same provider and pattern as row 1.",
    ]


def test_labels() -> None:
    assert version_label(INFO).startswith("Version 3 · editor · from ")
    assert version_label(INFO).endswith(" · cheaper")
    assert csv_filename(3) == "agentlens-pricing-v3.csv"


def test_unpriced_rows_most_used_first_req_171b() -> None:
    rows = unpriced_rows([
        UnpricedModel(provider="a", model="m1", calls=2, last_seen_ns=T0),
        UnpricedModel(provider="b", model="m2", calls=1200, last_seen_ns=T0),
    ])  # fmt: skip
    assert [(r.model, r.calls) for r in rows] == [("m2", "1,200"), ("m1", "2")]
