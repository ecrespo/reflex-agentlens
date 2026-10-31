"""Pricing resolution unit tests (REQ-061.a/b/c; Data Model §2.5 resolution order)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from reflex_agentlens.core.pricing import glob_matches, llm_cost, match_entry


@dataclass(frozen=True)
class E:
    model_pattern: str
    match_kind: str
    input_nusd_per_mtok: int = 1_000_000_000
    output_nusd_per_mtok: int = 2_000_000_000
    provider: str = ""
    cached_input_nusd_per_mtok: int | None = None
    cache_write_nusd_per_mtok: int | None = None


@pytest.mark.parametrize(
    ("pattern", "model", "ok"),
    [
        ("gpt-4o*", "gpt-4o-mini", True),
        ("gpt-4o*", "gpt-4", False),
        ("*-mini", "gpt-4o-mini", True),
        ("claude-*-sonnet*", "claude-3-5-sonnet-latest", True),
        ("a.b*", "aXb", False),  # '.' is literal
        ("GPT*", "gpt-4o", False),  # case-sensitive
        ("x[1]*", "x[1]y", True),
    ],
)
def test_glob(pattern: str, model: str, ok: bool) -> None:
    assert glob_matches(pattern, model) is ok


def test_response_model_before_request_model() -> None:
    """REQ-061.b."""
    a, b = E("gpt-4o", "exact"), E("gpt-4o-2024-08-06", "exact")
    assert match_entry([a, b], "openai", "gpt-4o-2024-08-06", "gpt-4o") is b
    assert match_entry([a], "openai", "gpt-4o-2024-08-06", "gpt-4o") is a  # falls back
    assert match_entry([a], "openai", None, "gpt-4o") is a


def test_exact_before_glob_and_provider_specific_first() -> None:
    glob_specific = E("gpt-4o*", "glob", provider="openai")
    exact_any = E("gpt-4o", "exact")
    exact_openai = E("gpt-4o", "exact", provider="openai")
    assert match_entry([glob_specific, exact_any], "openai", "gpt-4o", None) is exact_any
    assert match_entry([exact_any, exact_openai], "openai", "gpt-4o", None) is exact_openai
    assert match_entry([exact_openai], "azure", "gpt-4o", None) is None  # other provider


def test_longest_glob_prefix_wins() -> None:
    short, long_ = E("gpt*", "glob"), E("gpt-4o*", "glob")
    specific = E("gpt*", "glob", provider="openai")
    assert match_entry([short, long_], "openai", "gpt-4o-mini", None) is long_
    assert match_entry([short, specific], "openai", "gpt-x", None) is specific
    assert match_entry([short], None, "claude", None) is None


def test_cost_and_unpriced() -> None:
    """REQ-061.a/c: formula with cached subset; None without entry or tokens."""
    e = E("m", "exact", 2_500_000_000, 10_000_000_000, cached_input_nusd_per_mtok=1_250_000_000)
    # 800 uncached input + 200 cached input + 100 output tokens
    assert llm_cost(e, 1000, 100, 200) == (800 * 2_500_000_000 + 200 * 1_250_000_000
                                          + 100 * 10_000_000_000) // 1_000_000  # fmt: skip
    assert llm_cost(None, 1000, 100) is None
    assert llm_cost(e, None, 100) is None
    assert llm_cost(e, 10, None) is None
    assert llm_cost(e, 10, 5, 50) == llm_cost(e, 10, 5, 10)  # cached clamped to input
