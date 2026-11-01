"""Redaction editor and retention view model (plan task P6-10.3; REQ-039.f, 172, 173).

The redaction form is checked here for shape (rule names, empty fields, duplicates) and every
problem is listed at once; the regexes themselves are compiled and timed by the service with
the same engine ingest uses, and its message is turned into a row label
(:func:`rule_error_label`). Saving never rewrites stored spans (REQ-039.f,
:data:`NOT_REWRITTEN`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from reflex_agentlens.query.dto import (
    FieldRule,
    PatternRule,
    PurgeView,
    RedactionRuleSet,
    StorageStatus,
)
from reflex_agentlens.views.fmt import fmt_int, fmt_time

__all__ = [
    "MAX_RETENTION_DAYS",
    "NOT_REWRITTEN",
    "PRESETS",
    "FieldRow",
    "PatternRow",
    "StorageView",
    "fmt_bytes",
    "purge_label",
    "redaction_form",
    "retention_days",
    "rule_error_label",
    "rules_from_form",
    "storage_view",
]

NOT_REWRITTEN = (
    "Rule changes apply to spans received after you save. Data already stored is not rewritten."
)
PRESETS: dict[str, str] = {
    "secrets": "Secrets (API keys, tokens, private keys)",
    "email": "Email addresses",
    "phone": "Phone numbers",
    "credit_card": "Credit card numbers",
}
MAX_RETENTION_DAYS = 3650
_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
_WHERE = re.compile(r"^(pattern|field)_rules\[(\d+)\]$")


class PatternRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    regex: str = ""
    enabled: bool = True


class FieldRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    key_glob: str = ""
    action: str = "mask"


def redaction_form(rules: RedactionRuleSet) -> tuple[dict[str, bool], list[PatternRow],
                                                     list[FieldRow]]:  # fmt: skip
    presets = {name: bool(rules.presets.get(name, False)) for name in PRESETS}  # type: ignore[call-overload]
    patterns = [PatternRow(name=p.name, regex=p.regex, enabled=p.enabled)
                for p in rules.pattern_rules]  # fmt: skip
    fields = [FieldRow(key_glob=f.key_glob, action=f.action) for f in rules.field_rules]
    return presets, patterns, fields


def rules_from_form(
    presets: dict[str, bool], patterns: Sequence[PatternRow], fields: Sequence[FieldRow]
) -> tuple[RedactionRuleSet | None, list[str]]:
    """The edited rules, or every shape problem. Empty rows are ignored."""
    errors: list[str] = []
    out_patterns: list[PatternRule] = []
    names: dict[str, int] = {}
    for i, row in enumerate(patterns, start=1):
        name, source = row.name.strip(), row.regex
        if not name and not source.strip():
            continue
        where, before = f"Pattern rule {i}", len(errors)
        if not _NAME.match(name):
            errors.append(f"{where}: name must be 1-32 lowercase letters, digits or _.")
        elif name in names:
            errors.append(f"{where}: same name as pattern rule {names[name]}.")
        else:
            names[name] = i
        if not source.strip():
            errors.append(f"{where}: a regular expression is required.")
        if len(errors) == before:
            out_patterns.append(PatternRule(name=name, regex=source, enabled=row.enabled))
    out_fields: list[FieldRule] = []
    for i, frow in enumerate(fields, start=1):
        glob = frow.key_glob.strip()
        if not glob:
            continue
        where = f"Field rule {i}"
        if len(glob) > 256:
            errors.append(f"{where}: the attribute key is longer than 256 characters.")
        elif frow.action not in ("drop", "mask"):
            errors.append(f"{where}: the action must be drop or mask.")
        else:
            out_fields.append(FieldRule(key_glob=glob, action=frow.action))  # type: ignore[arg-type]
    if errors:
        return None, errors
    rules = RedactionRuleSet(
        presets={name: bool(presets.get(name, False)) for name in PRESETS},  # type: ignore[misc]
        pattern_rules=out_patterns,
        field_rules=out_fields,
    )
    return rules, []


def rule_error_label(message: str) -> str:
    """``Invalid pattern_rules[1]: …`` from the service → ``Pattern rule 2: …``."""
    head, sep, reason = message.removeprefix("Invalid ").partition(": ")
    match = _WHERE.match(head)
    if not sep or match is None:
        return message
    kind = "Pattern rule" if match.group(1) == "pattern" else "Field rule"
    return f"{kind} {int(match.group(2)) + 1}: {reason}"


# ── retention and storage (REQ-173) ──────────────────────────────────────────────────────────


def retention_days(text: str) -> tuple[int | None, str]:
    """The maximum age in days (0 keeps data forever), or a message."""
    text = text.strip()
    if not text.isdigit():
        return None, "Maximum age must be a whole number of days (0 keeps data forever)."
    days = int(text)
    if days > MAX_RETENTION_DAYS:
        return None, f"Maximum age is at most {fmt_int(MAX_RETENTION_DAYS)} days."
    return days, ""


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    raise AssertionError("unreachable")  # pragma: no cover


def purge_label(purge: PurgeView | None) -> str:
    """REQ-173.b: when the last purge ran and how many Traces it deleted."""
    if purge is None:
        return "No purge has run yet."
    traces = "1 Trace" if purge.deleted_traces == 1 else f"{fmt_int(purge.deleted_traces)} Traces"
    when = fmt_time(purge.finished_ns or purge.started_ns)
    if purge.error_class:
        return (
            f"{when} UTC ({purge.trigger}): failed ({purge.error_class}) after deleting {traces}."
        )
    if purge.finished_ns is None:
        return f"Started {when} UTC ({purge.trigger}), still running."
    return f"{when} UTC ({purge.trigger}): {traces} deleted."


@dataclass(frozen=True, slots=True)
class StorageView:
    usage: str
    limit: str
    counts: str
    last_purge: str


def storage_view(status: StorageStatus) -> StorageView:
    usage = f"{fmt_bytes(status.estimated_bytes)} estimated"
    if status.physical_bytes is not None:
        usage += f" ({fmt_bytes(status.physical_bytes)} on disk)"
    limit = fmt_bytes(status.size_limit_bytes) if status.size_limit_bytes else "No limit"
    counts = f"{fmt_int(status.runs)} Runs, {fmt_int(status.spans)} spans"
    return StorageView(usage, limit, counts, purge_label(status.last_purge))
