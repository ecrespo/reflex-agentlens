"""Attribute JSON codec: Data Model §1.4 (normative), plan task P2-01.1.

OTLP ``AnyValue`` ↔ JSON, stored per attribute key in one flat object::

    {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1234}

=================  =========================================================================
OTLP type          JSON
=================  =========================================================================
``string_value``   string
``bool_value``     ``true`` / ``false``
``int_value``      integer (full int64 range)
``double_value``   number; ``NaN`` / ``±Inf`` as the strings ``"NaN"``, ``"Infinity"``,
                   ``"-Infinity"``
``bytes_value``    ``{"$b64": "<base64>"}``
``array_value``    array (recursive)
``kvlist_value``   object (recursive, keys preserved)
(unset)            ``null``
=================  =========================================================================

Known ambiguities, all decoding to the *JSON-natural* type (the value itself is never lost):

- a non-finite double comes back as its string, and a ``kvlist`` whose only entry is a string
  under ``"$b64"`` comes back as bytes (both inherent to the normative table);
- PostgreSQL ``JSONB`` stores numbers as ``numeric`` and prints integral values without a decimal
  point, so on PG an integral double of magnitude ≥ 1e16 reads back as an integer and ``-0.0``
  as ``0.0``. An integer outside int64 can only have been a double and decodes as one.

Every other value round-trips exactly (REQ-030.b).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any, TypeAlias

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList

from reflex_agentlens.core.errors import ValidationError

__all__ = [
    "B64_KEY",
    "INT64_MAX",
    "INT64_MIN",
    "JsonValue",
    "any_value_to_json",
    "attributes_from_json",
    "attributes_to_json",
    "attrs_hash",
    "canonical_json",
    "decode_double",
    "json_to_any_value",
]

JsonValue: TypeAlias = "bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None"

B64_KEY = "$b64"
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
_NON_FINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def _double_to_json(value: float) -> JsonValue:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def any_value_to_json(value: AnyValue) -> JsonValue:
    """Encode one OTLP ``AnyValue`` per Data Model §1.4."""
    match value.WhichOneof("value"):
        case "string_value":
            return value.string_value
        case "bool_value":
            return value.bool_value
        case "int_value":
            return value.int_value
        case "double_value":
            return _double_to_json(value.double_value)
        case "bytes_value":
            return {B64_KEY: base64.b64encode(value.bytes_value).decode("ascii")}
        case "array_value":
            return [any_value_to_json(v) for v in value.array_value.values]
        case "kvlist_value":
            return attributes_to_json(value.kvlist_value.values)
        case _:
            return None


def attributes_to_json(attributes: Iterable[KeyValue]) -> dict[str, JsonValue]:
    """Encode an OTLP attribute list as one flat object; a repeated key keeps its last value."""
    return {kv.key: any_value_to_json(kv.value) for kv in attributes}


def json_to_any_value(value: JsonValue) -> AnyValue:
    """Decode the §1.4 JSON form back into an ``AnyValue``.

    Raises:
        ValidationError: on invalid base64 or a non-JSON type.
    """
    if value is None:
        return AnyValue()
    if isinstance(value, bool):  # before int: bool is an int subclass
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        if INT64_MIN <= value <= INT64_MAX:
            return AnyValue(int_value=value)
        return AnyValue(double_value=float(value))  # JSONB printed a large double as an integer
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, str):
        return AnyValue(string_value=value)
    if isinstance(value, list):
        return AnyValue(array_value=ArrayValue(values=[json_to_any_value(v) for v in value]))
    if isinstance(value, dict):
        b64 = value.get(B64_KEY)
        if len(value) == 1 and isinstance(b64, str):
            try:
                return AnyValue(bytes_value=base64.b64decode(b64, validate=True))
            except binascii.Error as exc:
                raise ValidationError("attribute", "invalid base64 in $b64") from exc
        return AnyValue(kvlist_value=KeyValueList(values=attributes_from_json(value)))
    raise ValidationError("attribute", f"unsupported JSON type {type(value).__name__}")


def attributes_from_json(attributes: Mapping[str, JsonValue]) -> list[KeyValue]:
    """Decode a flat attribute object into an OTLP attribute list (key order preserved)."""
    return [KeyValue(key=k, value=json_to_any_value(v)) for k, v in attributes.items()]


def decode_double(value: JsonValue) -> float | None:
    """Read a stored double that may carry a non-finite string marker; ``None`` if not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return _NON_FINITE.get(value)
    return None


def canonical_json(value: Any) -> str:
    """Canonical text: sorted keys, compact separators, UTF-8 kept, finite numbers only."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def attrs_hash(attributes: Mapping[str, JsonValue]) -> bytes:
    """BLAKE2b-128 of the canonical JSON (``resources``/``metric_points.attrs_hash``)."""
    return hashlib.blake2b(canonical_json(attributes).encode("utf-8"), digest_size=16).digest()
