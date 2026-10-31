"""core.ids: OTel ID conversion and validation, ULIDs (plan task P1-06.1).

REQ-010.a: all-zero and wrong-length trace/span IDs are invalid.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reflex_agentlens.core import ids
from reflex_agentlens.core.errors import ValidationError

TRACE_HEX = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_HEX = "00f067aa0ba902b7"


def test_trace_and_span_ids_round_trip_hex_bytes() -> None:
    assert ids.trace_id_to_hex(ids.trace_id_from_hex(TRACE_HEX)) == TRACE_HEX
    assert ids.span_id_to_hex(ids.span_id_from_hex(SPAN_HEX)) == SPAN_HEX
    assert ids.trace_id_from_hex(TRACE_HEX.upper()) == bytes.fromhex(TRACE_HEX)


@pytest.mark.parametrize(
    ("raw", "valid"),
    [
        (bytes(16), False),  # all-zero
        (b"\x01" * 15, False),  # too short
        (b"\x01" * 17, False),  # too long
        (b"", False),
        (b"\x00" * 15 + b"\x01", True),
    ],
)
def test_req_010a_trace_id_validation(raw: bytes, valid: bool) -> None:
    assert ids.is_valid_trace_id(raw) is valid


@pytest.mark.parametrize(
    ("raw", "valid"),
    [(bytes(8), False), (b"\x01" * 7, False), (b"\x01" * 9, False), (b"\x01" * 8, True)],
)
def test_req_010a_span_id_validation(raw: bytes, valid: bool) -> None:
    assert ids.is_valid_span_id(raw) is valid


@pytest.mark.parametrize("bad", ["0" * 32, TRACE_HEX[:-1], TRACE_HEX + "0", "zz" * 16, ""])
def test_req_010a_invalid_trace_id_hex_raises_naming_the_field(bad: str) -> None:
    with pytest.raises(ValidationError) as info:
        ids.trace_id_from_hex(bad)
    assert info.value.field == "trace_id"
    assert bad not in info.value.reason or bad == ""


@pytest.mark.parametrize("bad", ["0" * 16, SPAN_HEX[:-2], "g" * 16])
def test_req_010a_invalid_span_id_hex_raises(bad: str) -> None:
    with pytest.raises(ValidationError) as info:
        ids.span_id_from_hex(bad)
    assert info.value.field == "span_id"


@given(st.binary(min_size=16, max_size=16).filter(any))
def test_valid_trace_ids_round_trip(raw: bytes) -> None:
    assert ids.trace_id_from_hex(ids.trace_id_to_hex(raw)) == raw


def test_ulid_format_and_time_ordering() -> None:
    a = ids.new_ulid(now_ms=1_700_000_000_000)
    b = ids.new_ulid(now_ms=1_700_000_000_001)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", a)
    assert a < b
    assert ids.is_ulid(a)
    assert ids.ulid_timestamp_ms(a) == 1_700_000_000_000


def test_ulid_rejects_bad_strings() -> None:
    assert not ids.is_ulid("0" * 25)
    assert not ids.is_ulid("I" * 26)  # I is not in Crockford base32
    assert not ids.is_ulid("8" + "0" * 25)  # timestamp overflow (> 48 bits)


def test_ulids_are_unique() -> None:
    assert len({ids.new_ulid() for _ in range(1000)}) == 1000
