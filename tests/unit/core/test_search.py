"""Search normalization tests: DD-006 (Analyze A-15), REQ-073.b."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reflex_agentlens.core.search import search_normalize, search_tokens, truncate_utf8


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("execute_tool search_web", "execute tool search web"),
        ("db.query", "db query"),
        ("gpt-4o-mini", "gpt 4o mini"),
        ("RefundTool", "refundtool"),
        ("Résumé naïve CAFÉ", "resume naive cafe"),
        ("  --__..  ", ""),
        ("ﬁle", "file"),  # NFKD compatibility decomposition
    ],
)
def test_search_normalize_examples(text: str, expected: str) -> None:
    """REQ-073.b: the identifier cases where the backends disagreed before normalization."""
    assert search_normalize(text) == expected


@given(st.text())
def test_normalize_is_idempotent_and_tokens_are_word_only(text: str) -> None:
    once = search_normalize(text)
    assert search_normalize(once) == once
    for token in search_tokens(text):
        assert token
        assert all(c.isalnum() for c in token)


def test_query_with_operators_has_only_plain_tokens() -> None:
    assert search_tokens('"a" OR b* -c NEAR(d) & e | !f :g') == [
        "a", "or", "b", "c", "near", "d", "e", "f", "g",
    ]  # fmt: skip


def test_truncate_utf8_never_splits_a_character() -> None:
    assert truncate_utf8("abc", 3) == "abc"
    assert truncate_utf8("aé", 2) == "a"
    assert truncate_utf8("€€", 4) == "€"
