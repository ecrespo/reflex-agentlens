"""Attribute codec tests: plan task P2-01.1, Data Model §1.4, REQ-030.b."""

from __future__ import annotations

import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.store.attrs import (
    INT64_MAX,
    INT64_MIN,
    any_value_to_json,
    attributes_from_json,
    attributes_to_json,
    attrs_hash,
    canonical_json,
    decode_double,
    json_to_any_value,
)

# ── strategies: every AnyValue except the two documented lossy encodings ─────────────────────

scalars = st.one_of(
    st.just(AnyValue()),
    st.text().map(lambda v: AnyValue(string_value=v)),
    st.booleans().map(lambda v: AnyValue(bool_value=v)),
    st.integers(INT64_MIN, INT64_MAX).map(lambda v: AnyValue(int_value=v)),
    st.floats(allow_nan=False, allow_infinity=False).map(lambda v: AnyValue(double_value=v)),
    st.binary().map(lambda v: AnyValue(bytes_value=v)),
)
keys = st.text(max_size=20).filter(lambda k: k != "$b64")


def _array(children: st.SearchStrategy[AnyValue]) -> st.SearchStrategy[AnyValue]:
    return st.lists(children, max_size=4).map(
        lambda vs: AnyValue(array_value=ArrayValue(values=vs))
    )


def _kvlist(children: st.SearchStrategy[AnyValue]) -> st.SearchStrategy[AnyValue]:
    return st.dictionaries(keys, children, max_size=4).map(
        lambda d: AnyValue(
            kvlist_value=KeyValueList(values=[KeyValue(key=k, value=v) for k, v in d.items()])
        )
    )


any_values = st.recursive(scalars, lambda c: st.one_of(_array(c), _kvlist(c)), max_leaves=12)
attribute_lists = st.dictionaries(st.text(max_size=30), any_values, max_size=6).map(
    lambda d: [KeyValue(key=k, value=v) for k, v in d.items()]
)


@given(attribute_lists)
def test_attributes_round_trip_through_stored_json(attrs: list[KeyValue]) -> None:
    """REQ-030.b: the raw attribute view equals the OTLP input (through the stored JSON text)."""
    stored = json.dumps(attributes_to_json(attrs), allow_nan=False)
    assert attributes_from_json(json.loads(stored)) == attrs


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (AnyValue(string_value="gpt-4o"), "gpt-4o"),
        (AnyValue(bool_value=True), True),
        (AnyValue(int_value=INT64_MAX), INT64_MAX),
        (AnyValue(double_value=0.5), 0.5),
        (AnyValue(double_value=math.nan), "NaN"),
        (AnyValue(double_value=math.inf), "Infinity"),
        (AnyValue(double_value=-math.inf), "-Infinity"),
        (AnyValue(bytes_value=b"\x00\xff"), {"$b64": "AP8="}),
        (AnyValue(), None),
    ],
)
def test_encoding_table(value: AnyValue, encoded: object) -> None:
    """Data Model §1.4 table, row by row."""
    assert any_value_to_json(value) == encoded


def test_nested_values_encode_recursively() -> None:
    value = AnyValue(
        kvlist_value=KeyValueList(
            values=[
                KeyValue(key="a", value=AnyValue(array_value=ArrayValue(values=[AnyValue()]))),
                KeyValue(key="b", value=AnyValue(int_value=-1)),
            ]
        )
    )
    assert any_value_to_json(value) == {"a": [None], "b": -1}


def test_repeated_key_keeps_last_value() -> None:
    attrs = [
        KeyValue(key="k", value=AnyValue(int_value=1)),
        KeyValue(key="k", value=AnyValue(int_value=2)),
    ]
    assert attributes_to_json(attrs) == {"k": 2}


def test_non_finite_strings_decode_as_strings_and_as_doubles_on_request() -> None:
    assert json_to_any_value("NaN") == AnyValue(string_value="NaN")
    assert math.isnan(decode_double("NaN") or 0.0)
    assert decode_double("-Infinity") == -math.inf
    assert decode_double(3) == 3.0
    assert decode_double(True) is None
    assert decode_double("x") is None
    assert decode_double(None) is None


@pytest.mark.parametrize("value", [INT64_MAX + 1, INT64_MIN - 1, int(2.5e300)])
def test_int_outside_int64_decodes_as_double(value: int) -> None:
    """JSONB prints large doubles as integers; they can only have been doubles."""
    assert json_to_any_value(value) == AnyValue(double_value=float(value))


def test_invalid_base64_is_rejected() -> None:
    with pytest.raises(ValidationError, match="base64"):
        json_to_any_value({"$b64": "not base64!"})


def test_dict_with_extra_keys_is_a_kvlist_not_bytes() -> None:
    decoded = json_to_any_value({"$b64": "AA==", "x": 1})
    assert decoded.WhichOneof("value") == "kvlist_value"


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unsupported"):
        json_to_any_value((1, 2))  # type: ignore[arg-type]


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": ["é", None]}) == '{"a":["é",null],"b":1}'
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"x": math.nan})


@given(st.dictionaries(st.text(max_size=10), st.integers(), max_size=8))
def test_attrs_hash_ignores_key_order(attrs: dict[str, int]) -> None:
    reversed_attrs = dict(reversed(list(attrs.items())))
    digest = attrs_hash(attrs)
    assert len(digest) == 16
    assert attrs_hash(reversed_attrs) == digest


def test_attrs_hash_distinguishes_values() -> None:
    assert attrs_hash({"service.name": "a"}) != attrs_hash({"service.name": "b"})


@pytest.mark.pg
async def test_round_trip_through_postgres_jsonb(pg_engine: AsyncEngine) -> None:
    """REQ-030.b on PostgreSQL: values survive JSONB; only the documented type ambiguities apply."""
    attrs = {
        "s": "x", "i": INT64_MIN, "d": 0.1, "big": 2.5e300, "tiny": 1e-7, "nan": "NaN",
        "b": {"$b64": "AP8="}, "arr": [True, None, 1.5], "kv": {"k": [1, "2"]},
    }  # fmt: skip
    async with pg_engine.connect() as conn:
        stored = (
            await conn.execute(text("SELECT CAST(:v AS jsonb)"), {"v": json.dumps(attrs)})
        ).scalar_one()
    decoded = attributes_to_json(attributes_from_json(stored))
    assert decoded == attrs
    assert json_to_any_value(stored["big"]).WhichOneof("value") == "double_value"
    assert json_to_any_value(stored["d"]).WhichOneof("value") == "double_value"
