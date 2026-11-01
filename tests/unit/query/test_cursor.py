"""Signed keyset cursors (plan task P4-01.3; API §6.2, REQ-100.a, REQ-105.a)."""

from __future__ import annotations

import base64
import json

import pytest

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.security import sign
from reflex_agentlens.query.cursor import CursorCodec
from reflex_agentlens.store.records import RunCursor, SessionCursor

KEY = b"k" * 32
RUN = RunCursor(sort_value=123, trace_id=bytes(range(1, 17)), project_id="p1")
SESSION = SessionCursor(sort_value=-1, session_id="s1")


def _field(fn: object, *args: object) -> str:
    with pytest.raises(ValidationError) as exc:
        fn(*args)  # type: ignore[operator]
    return exc.value.field


def test_run_cursor_round_trip() -> None:
    codec = CursorCodec(KEY)
    token = codec.encode_run("cost_desc", RUN)
    assert codec.decode_run("cost_desc", token) == RUN


def test_session_cursor_round_trip() -> None:
    codec = CursorCodec(KEY)
    assert codec.decode_session("runs_asc", codec.encode_session("runs_asc", SESSION)) == SESSION


def test_tampered_truncated_and_foreign_cursors_are_refused() -> None:
    codec = CursorCodec(KEY)
    token = codec.encode_run("start_desc", RUN)
    _, mac = token.split(".")
    forged_body = base64.urlsafe_b64encode(
        json.dumps(["run", "start_desc", 999, RUN.trace_id.hex(), "p1"]).encode()
    ).rstrip(b"=")
    cases = [
        token[:-3],
        f"{forged_body.decode()}.{mac}",
        "garbage",
        "",
        "a" * 2000,
        CursorCodec(b"x" * 32).encode_run("start_desc", RUN),
    ]
    for bad in cases:
        assert _field(codec.decode_run, "start_desc", bad) == "cursor"


def test_cursor_of_another_sort_or_kind_is_refused() -> None:
    codec = CursorCodec(KEY)
    token = codec.encode_run("start_desc", RUN)
    assert _field(codec.decode_run, "start_asc", token) == "cursor"
    session_token = codec.encode_session("last_desc", SESSION)
    assert _field(codec.decode_run, "last_desc", session_token) == "cursor"


@pytest.mark.parametrize(
    "payload",
    [
        {"x": 1},
        ["run", "start_desc"],
        ["run", "start_desc", "1", "00", "p"],
        ["run", "start_desc", True, "00", "p"],
        ["run", "start_desc", 1, "zz", "p"],
        ["run", "start_desc", 1, "00" * 16, "p"],  # all-zero trace ID is invalid
    ],
)
def test_signed_but_malformed_payloads_are_refused(payload: object) -> None:
    token = sign(KEY, "cursor", json.dumps(payload).encode())
    assert _field(CursorCodec(KEY).decode_run, "start_desc", token) == "cursor"


def test_short_key_is_refused() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        CursorCodec(b"short")
