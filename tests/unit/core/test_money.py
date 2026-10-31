"""core.money: Decimal <-> nano-USD, per-Mtok prices, LLM call cost (plan task P1-06.2).

Constitution Art. 5 (interpretation N3): money is a fixed-point integer of nano-USD in storage and a
Decimal in Python; float is refused everywhere.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reflex_agentlens.core import money
from reflex_agentlens.core.errors import ValidationError


def test_usd_to_nusd_exact() -> None:
    assert money.usd_to_nusd(Decimal("0.000000001")) == 1
    assert money.usd_to_nusd(Decimal("2.5")) == 2_500_000_000
    assert money.usd_to_nusd("0.15") == 150_000_000
    assert money.usd_to_nusd(3) == 3_000_000_000


@pytest.mark.parametrize(
    ("usd", "nusd"),
    [
        ("0.0000000005", 0),  # half -> even (0)
        ("0.0000000015", 2),  # half -> even (2)
        ("0.0000000025", 2),
        ("0.00000000251", 3),
        ("-0.0000000015", -2),
    ],
)
def test_usd_to_nusd_rounds_half_even(usd: str, nusd: int) -> None:
    assert money.usd_to_nusd(Decimal(usd)) == nusd


@given(st.integers(min_value=-(10**18), max_value=10**18))
def test_nusd_round_trip_is_exact(nusd: int) -> None:
    usd = money.nusd_to_usd(nusd)
    assert isinstance(usd, Decimal)
    assert money.usd_to_nusd(usd) == nusd


@given(st.decimals(min_value=-(10**9), max_value=10**9, places=9, allow_nan=False))
def test_decimal_round_trip_with_nine_places_is_exact(usd: Decimal) -> None:
    assert money.nusd_to_usd(money.usd_to_nusd(usd)) == usd


@pytest.mark.parametrize("bad", [0.1, 1e-9, float("nan")])
def test_float_inputs_are_refused(bad: float) -> None:
    with pytest.raises(TypeError):
        money.usd_to_nusd(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        money.price_per_mtok_to_nusd(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "abc", ""])
def test_non_finite_or_malformed_decimals_are_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        money.usd_to_nusd(bad)


def test_bool_is_not_money() -> None:
    with pytest.raises(TypeError):
        money.usd_to_nusd(True)


def test_price_per_mtok_conversion() -> None:
    # $0.15 per 1M tokens is stored as 150,000,000 nano-USD per Mtok.
    assert money.price_per_mtok_to_nusd(Decimal("0.15")) == 150_000_000
    assert money.nusd_per_mtok_to_price(150_000_000) == Decimal("0.15")


def test_req_061a_llm_call_cost_formula() -> None:
    # 1,000 input of which 200 cached, 500 output; $2.50 / $10.00 / $1.25 per Mtok.
    p_in, p_out, p_cached = 2_500_000_000, 10_000_000_000, 1_250_000_000
    cost = money.llm_call_cost_nusd(
        input_tokens=1000,
        output_tokens=500,
        cached_input_tokens=200,
        p_in=p_in,
        p_out=p_out,
        p_cached=p_cached,
    )
    # 800 x 2.5 + 200 x 1.25 + 500 x 10 = 2000 + 250 + 5000 = 7250 USD per Mtok-token -> /1e6
    assert cost == 7_250_000
    assert money.nusd_to_usd(cost) == Decimal("0.00725")


def test_req_061a_cached_falls_back_to_input_price_and_cache_write_priced() -> None:
    cost = money.llm_call_cost_nusd(
        input_tokens=10,
        output_tokens=0,
        cached_input_tokens=4,
        p_in=1_000_000,
        p_out=0,
        p_cached=None,
        cache_write_tokens=2,
        p_cache_write=None,
    )
    assert cost == 12  # (6 + 4 + 2) x 1,000,000 / 1,000,000


def test_cost_rounds_half_even_at_the_final_division() -> None:
    # 1 token x 500,000 nUSD/Mtok = 0.5 nUSD -> 0 ; 3 tokens -> 1.5 -> 2
    assert money.llm_call_cost_nusd(1, 0, 0, p_in=500_000, p_out=0) == 0
    assert money.llm_call_cost_nusd(3, 0, 0, p_in=500_000, p_out=0) == 2


def test_cost_rejects_inconsistent_token_counts() -> None:
    with pytest.raises(ValidationError):
        money.llm_call_cost_nusd(10, 0, 11, p_in=1, p_out=1)
    with pytest.raises(ValidationError):
        money.llm_call_cost_nusd(-1, 0, 0, p_in=1, p_out=1)


@pytest.mark.parametrize(
    ("nusd", "text"),
    [(0, "0"), (150_000_000, "0.15"), (3 * 10**9, "3"), (10**18, "1000000000"), (1, "1E-9")],
)
def test_nusd_to_usd_string_form(nusd: int, text: str) -> None:
    assert str(money.nusd_to_usd(nusd)) == text
