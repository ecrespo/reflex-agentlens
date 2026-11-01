"""``RunFilter`` and the Runs URL grammar (plan task P4-01.2; API §6.2, REQ-101, REQ-102.b).

:func:`parse_runs_query` turns URL parameters into a validated :class:`RunsQuery`;
:func:`runs_query_string` writes the canonical query string back (fixed parameter order,
defaults omitted). ``parse ∘ serialize`` is the identity. A malformed parameter raises
``ValidationError(field=<URL name>)``; nothing falls back to an unfiltered list (REQ-101.i).
Unknown parameters are ignored, so pages can add their own (e.g. ``replay``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, TypeVar, get_args
from urllib.parse import parse_qsl, quote, urlencode

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.ids import is_ulid
from reflex_agentlens.query.dto import (
    AttrPredicate,
    Range,
    RunSort,
    RunStatus,
    TimeExpr,
)
from reflex_agentlens.query.timeexpr import check_time_expr

__all__ = [
    "MAX_VALUES",
    "RunFilter",
    "RunsQuery",
    "format_attr",
    "parse_attr",
    "parse_runs_query",
    "runs_query_string",
]

N = TypeVar("N", int, Decimal)

MAX_VALUES: Final = 50  # per repeatable parameter
_MAX_NAME = 200
_MAX_SOURCE = 385  # namespace (128) + "/" + name (256)
_MAX_TEXT = 500
_MAX_CURSOR = 1024
_STATUSES: Final = frozenset(get_args(RunStatus))
_SORTS: Final = frozenset(get_args(RunSort))
_BOOL = {"1": True, "true": True, "0": False, "false": False}
_SINGLE = (
    "from", "to", "genai", "dur", "tok", "cost", "unpriced", "q", "session", "sort",
    "cursor", "saved",
)  # fmt: skip


class RunFilter(BaseModel):
    """API §6.2 (``sources`` holds Source labels, §6.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_ids: list[str] = []
    sources: list[str] = []
    time_from: TimeExpr | None = None
    time_to: TimeExpr | None = None
    statuses: list[RunStatus] = []
    is_genai: bool | None = None
    models: list[str] = []
    tools: list[str] = []
    duration_ms: Range[int] | None = None
    tokens: Range[int] | None = None
    cost_usd: Range[Decimal] | None = None
    include_unpriced_in_cost_range: bool = False
    attributes: list[AttrPredicate] = []
    text: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunsQuery:
    filter: RunFilter
    sort: RunSort = "start_desc"
    cursor: str | None = None
    saved: str | None = None


# ── value parsers ────────────────────────────────────────────────────────────────────────────


def _name(value: str, field: str, max_len: int = _MAX_NAME) -> str:
    if not value.strip() or len(value) > max_len or any(ord(c) < 32 for c in value):
        raise ValidationError(field, "empty, too long or contains control characters")
    return value


def _int(text: str, field: str) -> int:
    if not text.isdigit() or len(text) > 18:
        raise ValidationError(field, "expected a non-negative integer")
    return int(text)


def _decimal(text: str, field: str) -> Decimal:
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise ValidationError(field, "expected a non-negative decimal") from None
    if not d.is_finite() or d < 0 or len(text) > 32:
        raise ValidationError(field, "expected a non-negative decimal")
    return d


def _bounds(value: str, field: str, parse: Callable[[str, str], N]) -> tuple[N | None, N | None]:
    lo, sep, hi = value.partition("..")
    if not sep or (not lo and not hi):
        raise ValidationError(field, "expected min..max, ..max or min..")
    low = parse(lo, field) if lo else None
    high = parse(hi, field) if hi else None
    if low is not None and high is not None and low > high:
        raise ValidationError(field, "range minimum exceeds maximum")
    return low, high


def _int_range(value: str, field: str) -> Range[int]:
    low, high = _bounds(value, field, _int)
    return Range[int](min=low, max=high)


def _cost_range(value: str) -> Range[Decimal]:
    low, high = _bounds(value, "cost", _decimal)
    return Range[Decimal](min=low, max=high)


def parse_attr(value: str) -> AttrPredicate:
    """``[root:|resource:]key[=value]``; no ``=`` means existence."""
    scope = "root"
    head, colon, rest = value.partition(":")
    if colon and head in ("root", "resource"):
        scope, value = head, rest
    key, eq, raw = value.partition("=")
    try:
        if eq:
            return AttrPredicate(scope=scope, key=key, op="eq", value=raw)  # type: ignore[arg-type]
        return AttrPredicate(scope=scope, key=key, op="exists")  # type: ignore[arg-type]
    except PydanticValidationError:
        raise ValidationError("attr", "expected scope:key=value or scope:key") from None


def format_attr(pred: AttrPredicate) -> str:
    base = f"{pred.scope}:{pred.key}"
    return base if pred.op == "exists" else f"{base}={pred.value}"


def _fmt_number(d: int | Decimal) -> str:
    if isinstance(d, int):
        return str(d)
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fmt_range(rng: Range[int] | Range[Decimal] | None) -> str | None:
    if rng is None:
        return None
    lo = "" if rng.min is None else _fmt_number(rng.min)
    hi = "" if rng.max is None else _fmt_number(rng.max)
    return f"{lo}..{hi}"


# ── parse / serialize ────────────────────────────────────────────────────────────────────────


def _pairs(
    params: str | Mapping[str, Sequence[str]] | Iterable[tuple[str, str]],
) -> dict[str, list[str]]:
    if isinstance(params, str):
        items: Iterable[tuple[str, str]] = parse_qsl(params.lstrip("?"), keep_blank_values=True)
    elif isinstance(params, Mapping):
        items = [(k, v) for k, vs in params.items() for v in vs]
    else:
        items = params
    out: dict[str, list[str]] = {}
    for key, value in items:
        out.setdefault(key, []).append(value)
    return out


def parse_runs_query(
    params: str | Mapping[str, Sequence[str]] | Iterable[tuple[str, str]],
) -> RunsQuery:
    """URL parameters → :class:`RunsQuery` (API §6.2 table; REQ-101.i)."""
    p = _pairs(params)
    for key in _SINGLE:
        if len(p.get(key, ())) > 1:
            raise ValidationError(key, "given more than once")
    for key, values in p.items():
        if len(values) > MAX_VALUES:
            raise ValidationError(key, f"more than {MAX_VALUES} values")

    def one(key: str) -> str | None:
        values = p.get(key)
        return values[0] if values else None

    def many(key: str, max_len: int = _MAX_NAME) -> list[str]:
        return [_name(v, key, max_len) for v in p.get(key, [])]

    statuses = many("status")
    for st in statuses:
        if st not in _STATUSES:
            raise ValidationError("status", "unknown status")
    genai = one("genai")
    if genai is not None and genai not in _BOOL:
        raise ValidationError("genai", "expected 1 or 0")
    unpriced = one("unpriced")
    if unpriced is not None and unpriced not in _BOOL:
        raise ValidationError("unpriced", "expected 1 or 0")
    sort = one("sort") or "start_desc"
    if sort not in _SORTS:
        raise ValidationError("sort", "unknown sort")
    session, saved, cursor = one("session"), one("saved"), one("cursor")
    if session is not None and not is_ulid(session):
        raise ValidationError("session", "expected a Session ID")
    if saved is not None and not is_ulid(saved):
        raise ValidationError("saved", "expected a saved-filter ID")
    if cursor is not None and not 0 < len(cursor) <= _MAX_CURSOR:
        raise ValidationError("cursor", "malformed")
    time_from, time_to, text = one("from"), one("to"), one("q")
    dur, tok, cost = one("dur"), one("tok"), one("cost")
    filt = RunFilter(
        project_ids=many("project"),
        sources=many("source", _MAX_SOURCE),
        time_from=None if time_from is None else check_time_expr(time_from, "from"),
        time_to=None if time_to is None else check_time_expr(time_to, "to"),
        statuses=statuses,  # type: ignore[arg-type]
        is_genai=None if genai is None else _BOOL[genai],
        models=many("model"),
        tools=many("tool"),
        duration_ms=None if dur is None else _int_range(dur, "dur"),
        tokens=None if tok is None else _int_range(tok, "tok"),
        cost_usd=None if cost is None else _cost_range(cost),
        include_unpriced_in_cost_range=bool(unpriced is not None and _BOOL[unpriced]),
        attributes=[parse_attr(v) for v in p.get("attr", [])],
        text=None if text is None else _name(text, "q", _MAX_TEXT),
        session_id=session,
    )
    return RunsQuery(filter=filt, sort=sort, cursor=cursor, saved=saved)  # type: ignore[arg-type]


def runs_query_string(
    f: RunFilter,
    sort: RunSort = "start_desc",
    cursor: str | None = None,
    saved: str | None = None,
) -> str:
    """Canonical query string (fixed order, defaults omitted) for links and saved filters."""
    pairs: list[tuple[str, str]] = []
    pairs += [("project", v) for v in f.project_ids]
    pairs += [("source", v) for v in f.sources]
    if f.time_from is not None:
        pairs.append(("from", f.time_from))
    if f.time_to is not None:
        pairs.append(("to", f.time_to))
    pairs += [("status", v) for v in f.statuses]
    if f.is_genai is not None:
        pairs.append(("genai", "1" if f.is_genai else "0"))
    pairs += [("model", v) for v in f.models]
    pairs += [("tool", v) for v in f.tools]
    for key, rng in (("dur", f.duration_ms), ("tok", f.tokens), ("cost", f.cost_usd)):
        text = _fmt_range(rng)
        if text is not None:
            pairs.append((key, text))
    if f.include_unpriced_in_cost_range:
        pairs.append(("unpriced", "1"))
    pairs += [("attr", format_attr(a)) for a in f.attributes]
    if f.text is not None:
        pairs.append(("q", f.text))
    if f.session_id is not None:
        pairs.append(("session", f.session_id))
    if sort != "start_desc":
        pairs.append(("sort", sort))
    if cursor is not None:
        pairs.append(("cursor", cursor))
    if saved is not None:
        pairs.append(("saved", saved))
    return urlencode(pairs, quote_via=quote, safe="")
