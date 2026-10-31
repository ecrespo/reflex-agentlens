"""Money as exact fixed point (Constitution Art. 5, interpretation N3; Data Model §1.3, §2.5).

- Storage: ``int`` nano-USD (1 nUSD = 10⁻⁹ USD); prices: ``int`` nano-USD per million tokens.
- Python: ``decimal.Decimal`` USD. ``float`` is refused with ``TypeError`` everywhere.
- Rounding rule: **round half to even**, applied once — when a Decimal with more than nine
  decimal places is converted to nano-USD, and at the final division of the cost formula.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from reflex_agentlens.core.errors import ValidationError

__all__ = [
    "NUSD_PER_USD",
    "TOKENS_PER_MTOK",
    "llm_call_cost_nusd",
    "nusd_per_mtok_to_price",
    "nusd_to_usd",
    "price_per_mtok_to_nusd",
    "usd_to_nusd",
]

NUSD_PER_USD = 10**9
TOKENS_PER_MTOK = 10**6
_NANO = Decimal(1).scaleb(-9)

MoneyInput = Decimal | int | str


def _to_decimal(value: MoneyInput, field: str) -> Decimal:
    if isinstance(value, float | bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"{field}: money must be Decimal, int or str, not {type(value).__name__}")
    try:
        d = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation:
        raise ValidationError(field, "not a decimal number") from None
    if not d.is_finite():
        raise ValidationError(field, "not a finite number")
    return d


def usd_to_nusd(value: MoneyInput) -> int:
    """USD amount → integer nano-USD (round half to even beyond nine decimals)."""
    d = _to_decimal(value, "amount")
    return int(d.quantize(_NANO, rounding=ROUND_HALF_EVEN).scaleb(9))


def nusd_to_usd(nusd: int) -> Decimal:
    """Integer nano-USD → exact Decimal USD, without trailing zeros or exponent notation."""
    if isinstance(nusd, bool) or not isinstance(nusd, int):
        raise TypeError("nano-USD must be int")
    d = Decimal(nusd).scaleb(-9).normalize()
    return d.quantize(Decimal(1)) if d.as_tuple().exponent > 0 else d  # type: ignore[operator]


def price_per_mtok_to_nusd(price_usd_per_mtok: MoneyInput) -> int:
    """USD per 1M tokens → nano-USD per 1M tokens (the stored ``*_nusd_per_mtok`` value)."""
    return usd_to_nusd(_to_decimal(price_usd_per_mtok, "price"))


def nusd_per_mtok_to_price(nusd_per_mtok: int) -> Decimal:
    return nusd_to_usd(nusd_per_mtok)


def _div_round_half_even(num: int, den: int) -> int:
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2):
        q += 1
    return q


def llm_call_cost_nusd(
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    *,
    p_in: int,
    p_out: int,
    p_cached: int | None = None,
    cache_write_tokens: int = 0,
    p_cache_write: int | None = None,
) -> int:
    """Cost of one LLM call in nano-USD [REQ-061.a; Data Model §2.5 cost formula].

    Prices are nano-USD per million tokens. ``cached_input_tokens`` is a subset of
    ``input_tokens`` (true for every mapped dialect); a missing cached or cache-write price falls
    back to the input price. Integer arithmetic, one round-half-even division at the end.
    """
    counts = (input_tokens, output_tokens, cached_input_tokens, cache_write_tokens)
    if any(type(c) is not int or c < 0 for c in counts):  # bool is not a token count
        raise ValidationError("tokens", "token counts must be non-negative integers")
    if cached_input_tokens > input_tokens:
        raise ValidationError("cached_input_tokens", "exceeds input_tokens")
    non_cached = input_tokens - cached_input_tokens
    total = (
        non_cached * p_in
        + cached_input_tokens * (p_in if p_cached is None else p_cached)
        + output_tokens * p_out
        + cache_write_tokens * (p_in if p_cache_write is None else p_cache_write)
    )
    return _div_round_half_even(total, TOKENS_PER_MTOK)
