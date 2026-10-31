"""core.logging: JSON logs with a content guard (plan task P1-08.1, DD-020, Tech Design §7.1)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Literal

import pytest

from reflex_agentlens.core import logging as al_logging
from reflex_agentlens.core.errors import StorageUnavailable

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
    assert second["event"] == "worker 1 started"  # allowlisted third-party args are rendered


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
        raise StorageUnavailable(f"pool exhausted after {len(local_secret)} tries")

    try:
        fails()
    except StorageUnavailable:
        log.exception("db.error")
    line = _lines(capture)[0]
    assert "Traceback" in str(line["traceback"])
    assert CANARY not in json.dumps(line)
    # AgentlensError messages are value-free by design, so they are kept.
    assert line["error"] == f"storage unavailable: pool exhausted after {len(CANARY)} tries"


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_req_323a_foreign_exception_messages_fail_closed(capture: io.StringIO, level: str) -> None:
    """A database driver error can embed bound parameters: only its class name is logged."""
    log = _setup(capture, level=level)
    try:
        try:
            raise KeyError(CANARY)
        except KeyError as inner:
            err = RuntimeError(f"INSERT failed, parameters: ('{CANARY}',)")
            err.add_note(CANARY)
            raise err from inner
    except RuntimeError:
        log.exception("write.failed")
    out = capture.getvalue()
    assert CANARY not in out
    line = _lines(capture)[0]
    assert line["error_class"] == "RuntimeError"
    assert "error" not in line
    if level == "DEBUG":
        assert "KeyError" in str(line["traceback"])  # the chain is shown, by class name


def test_req_323a_exception_group_members_are_guarded(capture: io.StringIO) -> None:
    log = _setup(capture, level="DEBUG")
    try:
        _raise_from_normalizer()
    except ValueError as e:
        group = ExceptionGroup("batch", [e, StorageUnavailable(CANARY)])
    try:
        raise group
    except ExceptionGroup:
        log.exception("batch.failed")
    assert CANARY not in capture.getvalue()
    assert "ValueError" in str(_lines(capture)[0]["traceback"])


def test_req_323a_args_dropped_for_content_bearing_third_party_loggers(
    capture: io.StringIO,
) -> None:
    _setup(capture)
    logging.getLogger("sqlalchemy.engine.Engine").warning("[generated] %r", (CANARY,))
    logging.getLogger("httpx").warning("HTTP Request: %s", CANARY)
    logging.getLogger("granian.server").warning("Listening at: %s", "http://127.0.0.1:8000")
    out = capture.getvalue()
    assert CANARY not in out
    assert "Listening at: http://127.0.0.1:8000" in out


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
