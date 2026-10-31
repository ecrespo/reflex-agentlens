"""Storage record tests: plan task P2-01.2 (API §5, Data Model §2)."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import Table

from reflex_agentlens.store import records as r
from reflex_agentlens.store import schema as s

KEYS = {"pk", "span_pk", "run_pk", "project_pk"}


def _fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _columns(table: Table, *, drop: set[str] = KEYS) -> set[str]:
    return {c.name for c in table.columns} - drop


@pytest.mark.parametrize(
    ("record", "table"),
    [
        (r.LLMCallRow, s.llm_calls),
        (r.ToolCallRow, s.tool_calls),
        (r.ApprovalRow, s.approvals),
        (r.GraphNodeRow, s.graph_nodes),
        (r.PricingEntry, s.pricing_entries),
        (r.PricingVersionMeta, s.pricing_versions),
        (r.PurgeReport, s.purge_log),
    ],
)
def test_row_records_match_their_table(record: type, table: Table) -> None:
    """A column added to the schema without its record field (or vice versa) fails here."""
    drop = KEYS | {"version"} if table is s.pricing_entries else KEYS
    assert _fields(record) == _columns(table, drop=drop)


def test_project_record_matches_table_with_public_id() -> None:
    assert _fields(r.Project) == _columns(s.projects)


def test_normalized_span_covers_every_span_column() -> None:
    by_reference = {"source_pk": "source", "resource_pk": "resource", "session_ext": "session"}
    expected = {by_reference.get(c, c) for c in _columns(s.spans)} | {"trace_id"}  # via run_pk
    derived = {"llm_call", "tool_call", "approval", "graph_node", "search_doc"}
    assert _fields(r.NormalizedSpan) - derived == expected


def test_metric_point_covers_every_column() -> None:
    expected = (_columns(s.metric_points) - {"source_pk"}) | {"source", "project_id"}
    assert _fields(r.MetricPoint) == expected


def _span(**kw: object) -> r.NormalizedSpan:
    base: dict[str, object] = {
        "trace_id": b"\1" * 16,
        "span_id": b"\2" * 8,
        "parent_span_id": None,
        "source": r.SourceKey("svc"),
        "resource": r.ResourceIn(b"\0" * 16, {}),
        "name": "n",
        "start_ns": 1,
        "end_ns": 2,
        "mapping_version": 1,
        "size_bytes": 10,
        "received_at_ns": 3,
    }
    return r.NormalizedSpan(**(base | kw))  # type: ignore[arg-type]


def test_normalized_span_accepts_a_valid_span() -> None:
    span = _span(parent_span_id=b"\3" * 8)
    assert span.category == "generic"
    assert span.attributes == {}


@pytest.mark.parametrize(
    "kw",
    [
        {"trace_id": b"\1" * 15},
        {"span_id": b"\1" * 9},
        {"parent_span_id": b"\1" * 7},
        {"start_ns": 5, "end_ns": 4},
    ],
)
def test_normalized_span_rejects_malformed_ids_and_times(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"bytes|precedes"):
        _span(**kw)


def test_records_are_frozen() -> None:
    key = r.RunKey("p", b"\0" * 16)
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.project_id = "q"  # type: ignore[misc]


def test_run_keys_sort_in_lock_order() -> None:
    """DD-004: locks are taken in (project, trace_id) order."""
    keys = [r.RunKey("b", b"\0" * 16), r.RunKey("a", b"\2" * 16), r.RunKey("a", b"\1" * 16)]
    assert [(k.project_id, k.trace_id[0]) for k in sorted(keys)] == [("a", 1), ("a", 2), ("b", 0)]


def test_time_range_rejects_inverted_bounds() -> None:
    assert r.TimeRange(1, 1).end_ns == 1
    with pytest.raises(ValueError, match="precedes"):
        r.TimeRange(2, 1)


def test_change_event_caps_run_keys_at_200() -> None:
    keys = [r.RunKey("p", i.to_bytes(16, "big")) for i in range(201)]
    event = r.ChangeEvent.build("spans_committed", "p", keys, 7)
    assert len(event.run_keys) == 200
    assert event.truncated
    assert not r.ChangeEvent.build("purged", "p", keys[:200], 7).truncated


def test_api_key_records_keep_hashes_out_of_repr() -> None:
    rec = r.ApiKeyRecord("abcd1234", "p", "", b"salt" * 4, b"h" * 32, 1, None, None, None)
    new = r.NewApiKeyRecord("abcd1234", "p", b"salt" * 4, b"h" * 32, 1)
    for text in (repr(rec), repr(new)):
        assert "abcd1234" in text
        assert "salt" not in text
        assert "hhhh" not in text


def test_schema_status_values() -> None:
    assert [m.value for m in r.SchemaStatus] == ["current", "outdated", "newer"]
