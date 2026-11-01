"""Redaction editor and retention view model (plan task P6-10.3; REQ-039.f, 172, 173)."""

from __future__ import annotations

import pytest

from reflex_agentlens.query.dto import (
    FieldRule,
    PatternRule,
    PurgeView,
    RedactionRuleSet,
    StorageStatus,
)
from reflex_agentlens.views.redaction import (
    NOT_REWRITTEN,
    FieldRow,
    PatternRow,
    fmt_bytes,
    purge_label,
    redaction_form,
    retention_days,
    rule_error_label,
    rules_from_form,
    storage_view,
)
from tests.unit.ui.factories import T0

RULES = RedactionRuleSet(
    presets={"secrets": True, "email": False, "phone": False, "credit_card": False},
    pattern_rules=[PatternRule(name="order_id", regex=r"ORD-\d+", enabled=False)],
    field_rules=[FieldRule(key_glob="http.request.header.*", action="drop")],
)


def test_form_round_trip_req_172a() -> None:
    presets, patterns, fields = redaction_form(RULES)
    assert presets == {"secrets": True, "email": False, "phone": False, "credit_card": False}
    assert patterns == [PatternRow(name="order_id", regex=r"ORD-\d+", enabled=False)]
    assert fields == [FieldRow(key_glob="http.request.header.*", action="drop")]
    rules, errors = rules_from_form(presets, patterns, fields)
    assert errors == []
    assert rules == RULES


def test_empty_rows_are_ignored() -> None:
    rules, errors = rules_from_form({}, [PatternRow(), PatternRow(name=" ", regex=" ")],
                                    [FieldRow(key_glob="  ")])  # fmt: skip
    assert errors == []
    assert rules is not None
    assert (rules.pattern_rules, rules.field_rules) == ([], [])
    assert not any(rules.presets.values())


@pytest.mark.parametrize(
    ("patterns", "fields", "message"),
    [
        ([PatternRow(name="Bad Name", regex="x")], [],
         "Pattern rule 1: name must be 1-32 lowercase letters, digits or _."),
        ([PatternRow(name="a", regex="x"), PatternRow(name="a", regex="y")], [],
         "Pattern rule 2: same name as pattern rule 1."),
        ([PatternRow(name="a")], [], "Pattern rule 1: a regular expression is required."),
        ([], [FieldRow(key_glob="k" * 257)],
         "Field rule 1: the attribute key is longer than 256 characters."),
        ([], [FieldRow(key_glob="k", action="burn")],
         "Field rule 1: the action must be drop or mask."),
    ],
)  # fmt: skip
def test_invalid_rule_messages(
    patterns: list[PatternRow], fields: list[FieldRow], message: str
) -> None:
    rules, errors = rules_from_form({}, patterns, fields)
    assert rules is None
    assert errors == [message]


@pytest.mark.parametrize(
    ("message", "label"),
    [
        ("Invalid pattern_rules[1]: invalid regex (error).",
         "Pattern rule 2: invalid regex (error)."),
        ("Invalid field_rules[0]: needs a key_glob.", "Field rule 1: needs a key_glob."),
        ("Invalid sample: at most 64 KiB.", "Invalid sample: at most 64 KiB."),
        ("Settings require admin rights.", "Settings require admin rights."),
    ],
)  # fmt: skip
def test_service_messages_name_the_row(message: str, label: str) -> None:
    assert rule_error_label(message) == label


def test_not_rewritten_notice_req_039f() -> None:
    assert "not rewritten" in NOT_REWRITTEN


@pytest.mark.parametrize(
    ("text", "days", "problem"),
    [
        ("30", 30, ""),
        (" 0 ", 0, ""),
        ("3650", 3650, ""),
        ("3651", None, "Maximum age is at most 3,650 days."),
        ("-1", None, "Maximum age must be a whole number of days (0 keeps data forever)."),
        ("1.5", None, "Maximum age must be a whole number of days (0 keeps data forever)."),
        ("", None, "Maximum age must be a whole number of days (0 keeps data forever)."),
    ],
)
def test_retention_days_req_173a(text: str, days: int | None, problem: str) -> None:
    assert retention_days(text) == (days, problem)


def test_fmt_bytes() -> None:
    assert [fmt_bytes(n) for n in (None, 0, 999, 1500, 2_500_000, 3 * 10**12, 5 * 10**15)] == [
        "—", "0 B", "999 B", "1.5 KB", "2.5 MB", "3.0 TB", "5000.0 TB",
    ]  # fmt: skip


def purge(**kw: object) -> PurgeView:
    base: dict[str, object] = {
        "trigger": "scheduled",
        "started_ns": T0,
        "finished_ns": T0 + 5,
        "deleted_traces": 12,
        "deleted_bytes_est": 0,
        "error_class": None,
    }
    base.update(kw)
    return PurgeView(**base)  # type: ignore[arg-type]


def test_purge_label_req_173b() -> None:
    assert purge_label(None) == "No purge has run yet."
    assert purge_label(purge()).endswith(" UTC (scheduled): 12 Traces deleted.")
    assert purge_label(purge(deleted_traces=1, trigger="manual")).endswith(
        "(manual): 1 Trace deleted."
    )
    assert "still running" in purge_label(purge(finished_ns=None))
    failed = purge_label(purge(error_class="OperationalError", deleted_traces=3))
    assert failed.endswith("failed (OperationalError) after deleting 3 Traces.")


def test_storage_view_req_173a() -> None:
    status = StorageStatus(estimated_bytes=2_500_000, physical_bytes=None, size_limit_bytes=None,
                           runs=1200, spans=34000, last_purge=None)  # fmt: skip
    view = storage_view(status)
    assert (view.usage, view.limit) == ("2.5 MB estimated", "No limit")
    assert view.counts == "1,200 Runs, 34,000 spans"
    assert view.last_purge == "No purge has run yet."
    limited = storage_view(status.model_copy(update={"physical_bytes": 3_000_000,
                                                     "size_limit_bytes": 10**9}))  # fmt: skip
    assert (limited.usage, limited.limit) == ("2.5 MB estimated (3.0 MB on disk)", "1.0 GB")
