"""Structured JSON logging with a content guard (REQ-341.a, REQ-323.a, Tech Design DD-020, §7.1).

- One JSON object per line on stdout: ``ts``, ``level``, ``logger``, ``event`` plus allowlisted
  extra fields. ``console`` format renders the same data as text, for local development.
- Content guard, **fail-closed** (applied to a private copy of every record that reaches the
  agentlens handler):
  * extra fields outside :data:`ALLOWED_EXTRAS` are dropped;
  * ``%`` arguments are rendered only for the third-party loggers in :data:`ARGS_ALLOWED_LOGGERS`;
    every other logger (agentlens, SQLAlchemy, httpx, …) logs its message template only, so
    values travel only in allowlisted extras;
  * an exception message is logged only for :class:`~reflex_agentlens.core.errors.AgentlensError`
    subclasses (their messages are value-free by design) that were not raised inside
    ``ingest.normalize`` / ``ingest.redaction``. Every other exception — including database
    errors, whose messages can embed bound parameters — is reduced to its class name, in the
    ``error`` field, in tracebacks, in chained causes, in exception-group members and in notes;
  * tracebacks are emitted only at ``DEBUG`` and never include local variables.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import IO, Any, Literal

from reflex_agentlens.core.errors import AgentlensError

__all__ = [
    "ALLOWED_EXTRAS",
    "ARGS_ALLOWED_LOGGERS",
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
#: Third-party loggers whose ``%`` arguments are operational (ports, worker numbers, revisions).
ARGS_ALLOWED_LOGGERS = ("granian", "alembic")
SENSITIVE_MODULES = ("reflex_agentlens.ingest.normalize", "reflex_agentlens.ingest.redaction")
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)
_HANDLER_ATTR = "_agentlens_handler"
_MAX_CHAIN = 16


def _under(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _related(exc: BaseException) -> list[BaseException]:
    """``exc`` plus its causes, contexts and exception-group members (bounded, cycle-safe)."""
    out: list[BaseException] = []
    seen: set[int] = set()
    work = [exc]
    while work and len(out) < 4 * _MAX_CHAIN:
        current = work.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        out.append(current)
        work.extend(e for e in (current.__cause__, current.__context__) if e is not None)
        if isinstance(current, BaseExceptionGroup):
            work.extend(current.exceptions)
    return out


def _raised_in_sensitive_module(exc: BaseException) -> bool:
    for current in _related(exc):
        tb = current.__traceback__
        while tb is not None:
            if _under(str(tb.tb_frame.f_globals.get("__name__", "")), SENSITIVE_MODULES):
                return True
            tb = tb.tb_next
    return False


def _message_is_safe(exc: BaseException, sensitive_origin: bool) -> bool:
    return isinstance(exc, AgentlensError) and not sensitive_origin


class ContentGuardFilter(logging.Filter):
    """Strips everything that could carry content before a record is formatted (REQ-323.a)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS]:
            if key not in ALLOWED_EXTRAS:
                delattr(record, key)
        if record.args and not _under(record.name, ARGS_ALLOWED_LOGGERS):
            record.msg, record.args = str(record.msg), None
        if record.exc_info and record.exc_info[1] is not None:
            record.error_class = type(record.exc_info[1]).__name__
        record.exc_text = None
        record.stack_info = None
        return True


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _format_traceback(exc: BaseException, sensitive_origin: bool) -> str:
    """Stack frames (no locals) of ``exc`` and its chain; messages only where safe."""
    blocks: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(blocks) < _MAX_CHAIN:
        seen.add(id(current))
        stack = traceback.StackSummary.extract(
            traceback.walk_tb(current.__traceback__), capture_locals=False
        )
        head = type(current).__qualname__
        if _message_is_safe(current, sensitive_origin):
            head += f": {current}"
        if isinstance(current, BaseExceptionGroup):
            head += " [" + ", ".join(type(e).__qualname__ for e in current.exceptions) + "]"
        blocks.append("Traceback (most recent call last):\n" + "".join(stack.format()) + head)
        if current.__cause__ is not None:
            current = current.__cause__
            blocks.append("The above exception was the direct cause of the following exception:")
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
            blocks.append("During handling of the above exception, another exception occurred:")
        else:
            current = None
    return "\n\n".join(reversed(blocks))


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
            exc = record.exc_info[1]
            sensitive_origin = _raised_in_sensitive_module(exc)
            if _message_is_safe(exc, sensitive_origin):
                out["error"] = str(exc)
            if self.tracebacks:
                out["traceback"] = _format_traceback(exc, sensitive_origin)
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
