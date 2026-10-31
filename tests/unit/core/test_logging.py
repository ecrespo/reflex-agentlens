"""core.logging: JSON logs with a content guard (plan task P1-08.1, DD-020, Tech Design §7.1)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Literal

import pytest

from reflex_agentlens.core import logging as al_logging

CANARY = "CANARY-7f3a9c-do-not-log"
NORMALIZER = "reflex_agentlens.ingest.normalize.fake"


@pytest.fixture
def capture() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    yield stream
    al_logging.reset_logging()


def _setup(
    stream: io.StringIO, level: str = "INFO", fmt: Literal["json", "console"] = "json"
) -> logging.Logger:
    al_logging.configure_logging(level=level, fmt=fmt, stream=stream)
    return logging.getLogger("agentlens.ingest")


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _raise_from_normalizer() -> None:
    code = f"def boom():\n    raise ValueError('bad attribute value: {CANARY}')\n"
    scope: dict[str, object] = {"__name__": NORMALIZER}
    exec(compile(code, "<normalizer>", "exec"), scope)  # noqa: S102 - simulates a module frame
    scope["boom"]()  # type: ignore[operator]


def test_req_341a_one_valid_json_object_per_line_with_required_fields(
    capture: io.StringIO,
) -> None:
    log = _setup(capture)
    log.info("export.accepted", extra={"project_id": "01JB", "spans": 512, "duration_ms": 61})
    logging.getLogger("granian").warning("worker %s started", 1)
    first, second = _lines(capture)
    assert {"ts", "level", "logger", "event"} <= first.keys()
    assert first["level"] == "INFO"
    assert first["logger"] == "agentlens.ingest"
    assert first["event"] == "export.accepted"
    assert (first["spans"], first["duration_ms"], first["project_id"]) == (512, 61, "01JB")
    assert str(first["ts"]).endswith("Z")
    assert second["event"] == "worker 1 started"  # third-party args still rendered


def test_req_341a_level_comes_from_config(capture: io.StringIO) -> None:
    log = _setup(capture, level="WARNING")
    log.info("hidden")
    log.warning("shown")
    assert [line["event"] for line in _lines(capture)] == ["shown"]


def test_req_323a_non_allowlisted_extras_are_dropped(capture: io.StringIO) -> None:
    log = _setup(capture)
    log.info("span.normalized", extra={"attribute_key": "gen_ai.prompt", "value": CANARY})
    out = capture.getvalue()
    assert CANARY not in out
    assert _lines(capture)[0]["attribute_key"] == "gen_ai.prompt"


def test_req_323a_agentlens_message_args_are_never_rendered(capture: io.StringIO) -> None:
    log = _setup(capture)
    log.info("tool.result %s", CANARY)
    assert CANARY not in capture.getvalue()


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_req_323a_normalizer_exception_message_never_logged(
    capture: io.StringIO, level: str
) -> None:
    log = _setup(capture, level=level)
    try:
        _raise_from_normalizer()
    except ValueError:
        log.exception("span.normalize_failed", extra={"span_id": "00f067aa0ba902b7"})
    out = capture.getvalue()
    assert CANARY not in out
    line = _lines(capture)[0]
    assert line["error_class"] == "ValueError"
    assert ("traceback" in line) is (level == "DEBUG")


def test_req_323a_canary_absent_in_console_format_too(capture: io.StringIO) -> None:
    log = _setup(capture, level="DEBUG", fmt="console")
    log.info("x %s", CANARY, extra={"value": CANARY, "spans": 2})
    try:
        _raise_from_normalizer()
    except ValueError:
        log.exception("span.normalize_failed")
    out = capture.getvalue()
    assert CANARY not in out
    assert "spans=2" in out


def test_tracebacks_only_at_debug_and_without_locals(capture: io.StringIO) -> None:
    log = _setup(capture, level="DEBUG")

    def fails() -> None:
        local_secret = CANARY  # a local variable: never rendered
        raise RuntimeError("database unreachable " + str(len(local_secret)))

    try:
        fails()
    except RuntimeError:
        log.exception("db.error")
    line = _lines(capture)[0]
    assert "Traceback" in str(line["traceback"])
    assert CANARY not in json.dumps(line)
    assert (
        line["error"] == f"database unreachable {len(CANARY)}"
    )  # non-normalizer messages are kept


def test_exception_at_info_has_class_but_no_traceback(capture: io.StringIO) -> None:
    log = _setup(capture)
    try:
        raise KeyError("missing")
    except KeyError:
        log.exception("lookup.failed")
    line = _lines(capture)[0]
    assert line["error_class"] == "KeyError"
    assert "traceback" not in line


def test_guard_does_not_mutate_records_seen_by_other_handlers(capture: io.StringIO) -> None:
    seen: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    other = Keep()
    logging.getLogger().addHandler(other)
    try:
        _setup(capture).info("x %s", "arg", extra={"value": "host-data"})
    finally:
        logging.getLogger().removeHandler(other)
    assert seen[0].args == ("arg",)
    assert seen[0].value == "host-data"  # type: ignore[attr-defined]
