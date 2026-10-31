"""Structured JSON logging with a content guard (REQ-341.a, REQ-323.a, Tech Design DD-020, §7.1).

- One JSON object per line on stdout: ``ts``, ``level``, ``logger``, ``event`` plus allowlisted
  extra fields. ``console`` format renders the same data as text, for local development.
- Content guard (applied to every record that reaches the agentlens handler):
  * extra fields outside :data:`ALLOWED_EXTRAS` are dropped;
  * for agentlens loggers the message template is logged and its ``%`` arguments are never
    rendered: values travel only in allowlisted extras;
  * an exception raised inside ``ingest.normalize`` or ``ingest.redaction`` is reduced to its class
    name, everywhere (message, traceback, chained causes);
  * tracebacks are emitted only at ``DEBUG`` and never include local variables.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from types import TracebackType
from typing import IO, Any, Literal

__all__ = [
    "ALLOWED_EXTRAS",
    "ContentGuardFilter",
    "JsonFormatter",
    "configure_logging",
    "reset_logging",
]

ALLOWED_EXTRAS = frozenset(
    {
        "project_id",
        "source_id",
        "trace_id",
        "span_id",
        "key_id",
        "protocol",
        "spans",
        "rejected",
        "duplicates",
        "bytes",
        "duration_ms",
        "queue_bytes",
        "status",
        "error_class",
        "attribute_key",
        "job",
        "deleted_traces",
        "request_id",
    }
)
AGENTLENS_LOGGERS = ("agentlens", "reflex_agentlens")
SENSITIVE_MODULES = ("reflex_agentlens.ingest.normalize", "reflex_agentlens.ingest.redaction")
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)
_GUARD_ATTR = "_agentlens_sensitive_exc"
_HANDLER_ATTR = "_agentlens_handler"

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def _is_agentlens(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in AGENTLENS_LOGGERS)


def _raised_in_sensitive_module(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        tb = current.__traceback__
        while tb is not None:
            module = str(tb.tb_frame.f_globals.get("__name__", ""))
            if any(module == m or module.startswith(m + ".") for m in SENSITIVE_MODULES):
                return True
            tb = tb.tb_next
        current = current.__cause__ or current.__context__
    return False


class ContentGuardFilter(logging.Filter):
    """Strips everything that could carry content before a record is formatted (REQ-323.a)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS]:
            if key not in ALLOWED_EXTRAS and key != _GUARD_ATTR:
                delattr(record, key)
        if _is_agentlens(record.name) and record.args:
            record.msg, record.args = str(record.msg), None
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            record.error_class = type(exc).__name__
            setattr(record, _GUARD_ATTR, _raised_in_sensitive_module(exc))
        record.exc_text = None
        record.stack_info = None
        return True


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    def __init__(self, *, tracebacks: bool, fmt: Literal["json", "console"] = "json") -> None:
        super().__init__()
        self.tracebacks = tracebacks
        self.fmt = fmt

    def _fields(self, record: logging.LogRecord) -> dict[str, Any]:
        ts = datetime.fromtimestamp(record.created, UTC)
        out: dict[str, Any] = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in sorted(ALLOWED_EXTRAS):
            if key in record.__dict__:
                out[key] = _jsonable(record.__dict__[key])
        if record.exc_info and record.exc_info[1] is not None:
            sensitive = bool(getattr(record, _GUARD_ATTR, True))
            exc_info: ExcInfo = record.exc_info
            if not sensitive:
                out["error"] = str(exc_info[1])
            if self.tracebacks:
                out["traceback"] = _format_traceback(exc_info, sensitive)
        return out

    def format(self, record: logging.LogRecord) -> str:
        fields = self._fields(record)
        if self.fmt == "json":
            return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        head = f"{fields.pop('ts')} {fields.pop('level'):<7} {fields.pop('logger')}: "
        head += str(fields.pop("event"))
        tb = fields.pop("traceback", None)
        text = head + "".join(f" {k}={v}" for k, v in fields.items())
        return f"{text}\n{tb}" if tb else text


def _format_traceback(exc_info: ExcInfo, sensitive: bool) -> str:
    exc_type, exc, tb = exc_info
    te = traceback.TracebackException(exc_type, exc, tb, capture_locals=False)
    if not sensitive:
        return "".join(te.format()).rstrip()
    buf = io.StringIO()
    buf.write("Traceback (most recent call last):\n")
    buf.writelines(te.stack.format())
    buf.write(f"{exc_type.__qualname__}\n")
    return buf.getvalue().rstrip()


class _GuardedStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Guards a private copy of each record, so other handlers (a host app's) see it unchanged."""

    def handle(self, record: logging.LogRecord) -> bool:
        return super().handle(logging.makeLogRecord(record.__dict__))


def configure_logging(
    level: str = "INFO",
    fmt: Literal["json", "console"] = "json",
    stream: IO[str] | None = None,
) -> logging.Handler:
    """Install the agentlens handler on the root logger (standalone ``serve``/``collect``).

    Replaces a handler installed by an earlier call; leaves other handlers alone.
    """
    reset_logging()
    handler = _GuardedStreamHandler(stream or sys.stdout)
    handler.addFilter(ContentGuardFilter())
    handler.setFormatter(JsonFormatter(tracebacks=level == "DEBUG", fmt=fmt))
    setattr(handler, _HANDLER_ATTR, True)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    return handler


def reset_logging() -> None:
    """Remove the handler installed by :func:`configure_logging`, if any."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_ATTR, False):
            root.removeHandler(handler)
            handler.close()
