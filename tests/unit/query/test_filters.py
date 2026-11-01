"""Runs URL grammar, time expressions and scope (plan task P4-01.2; API §6.2, REQ-101, 102.b)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reflex_agentlens.core.errors import Forbidden, ValidationError
from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.query.dto import AttrPredicate, Range, Viewer
from reflex_agentlens.query.filters import (
    RunFilter,
    parse_attr,
    parse_runs_query,
    runs_query_string,
)
from reflex_agentlens.query.scope import (
    MAX_NS,
    parse_source_label,
    resolve_projects,
    run_query,
    source_label,
    time_range,
)
from reflex_agentlens.query.timeexpr import resolve_time
from reflex_agentlens.store import records as r

NOW = 1_800_000_000 * 10**9
SID = new_ulid()


# ── time expressions ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("now", NOW),
        ("now-30s", NOW - 30 * 10**9),
        ("now-5m", NOW - 300 * 10**9),
        ("now-24h", NOW - 86_400 * 10**9),
        ("now-7d", NOW - 7 * 86_400 * 10**9),
        ("now-1w", NOW - 7 * 86_400 * 10**9),
        ("1970-01-01T00:00:01Z", 10**9),
        ("1970-01-01T01:00:01+01:00", 10**9),
    ],
)
def test_time_expressions_req_101b(expr: str, expected: int) -> None:
    assert resolve_time(expr, NOW, "from") == expected


@pytest.mark.parametrize(
    "expr", ["", "yesterday", "now-", "now-5y", "now+1h", "2026-10-30T12:00:00", "x" * 80]
)
def test_malformed_time_names_the_field_req_101i(expr: str) -> None:
    with pytest.raises(ValidationError) as exc:
        resolve_time(expr, NOW, "to")
    assert exc.value.field == "to"


def test_time_range_open_ends_and_order() -> None:
    assert time_range(None, None, NOW) is None
    assert time_range("now-1h", None, NOW) == r.TimeRange(NOW - 3600 * 10**9, MAX_NS)
    assert time_range(None, "now", NOW) == r.TimeRange(0, NOW)
    with pytest.raises(ValidationError) as exc:
        time_range("now", "now-1h", NOW)
    assert exc.value.field == "from"


# ── URL grammar ──────────────────────────────────────────────────────────────────────────────


def test_every_parameter_parses_api_6_2() -> None:
    q = parse_runs_query(
        "project=billing-bot&project=other&source=planner-svc&source=shop%2Frefund"
        "&from=now-24h&to=2026-10-30T12:00:00Z&status=error&status=incomplete&genai=1"
        "&model=gpt-4o&tool=search_web&dur=1000..30000&tok=..5000&cost=0.01..&unpriced=1"
        "&attr=root:user.tier=gold&attr=resource:deployment.environment&q=timeout%20refund"
        f"&session={SID}&sort=cost_desc&cursor=abc.def&saved={SID}&replay=ignored"
    )
    f = q.filter
    assert f.project_ids == ["billing-bot", "other"]
    assert f.sources == ["planner-svc", "shop/refund"]
    assert (f.time_from, f.time_to) == ("now-24h", "2026-10-30T12:00:00Z")
    assert f.statuses == ["error", "incomplete"]
    assert f.is_genai is True
    assert f.models == ["gpt-4o"]
    assert f.tools == ["search_web"]
    assert f.duration_ms == Range[int](min=1000, max=30000)
    assert f.tokens == Range[int](max=5000)
    assert f.cost_usd == Range[Decimal](min=Decimal("0.01"))
    assert f.include_unpriced_in_cost_range is True
    assert f.attributes == [
        AttrPredicate(scope="root", key="user.tier", op="eq", value="gold"),
        AttrPredicate(scope="resource", key="deployment.environment", op="exists"),
    ]
    assert f.text == "timeout refund"
    assert f.session_id == SID
    assert (q.sort, q.cursor, q.saved) == ("cost_desc", "abc.def", SID)


def test_empty_query_is_the_default_filter() -> None:
    q = parse_runs_query("")
    assert q.filter == RunFilter()
    assert q.sort == "start_desc"
    assert runs_query_string(q.filter) == ""


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("status=running", "status"),
        ("genai=yes", "genai"),
        ("unpriced=2", "unpriced"),
        ("sort=random", "sort"),
        ("dur=abc", "dur"),
        ("dur=5..1", "dur"),
        ("dur=..", "dur"),
        ("dur=-1..", "dur"),
        ("tok=1.5..", "tok"),
        ("cost=x..", "cost"),
        ("cost=-1..", "cost"),
        ("cost=NaN..", "cost"),
        ("from=yesterday", "from"),
        ("to=now-5y", "to"),
        ("attr=root:bad%20key=1", "attr"),
        ("attr=root:", "attr"),
        ("session=not-a-ulid", "session"),
        ("saved=1", "saved"),
        ("model=", "model"),
        ("q=", "q"),
        ("source=" + "x" * 400, "source"),
        ("from=now&from=now-1h", "from"),
        ("sort=start_asc&sort=cost_asc", "sort"),
        ("project=" + "&project=".join(["p"] * 51), "project"),
    ],
)
def test_malformed_parameter_names_its_field_req_101i(query: str, field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        parse_runs_query(query)
    assert exc.value.field == field


def test_attr_without_scope_is_root() -> None:
    assert parse_attr("user.tier=gold") == AttrPredicate(key="user.tier", value="gold")
    assert parse_attr("user.tier").op == "exists"


def test_mapping_and_pairs_inputs() -> None:
    assert parse_runs_query({"status": ["ok"]}).filter.statuses == ["ok"]
    assert parse_runs_query([("model", "m")]).filter.models == ["m"]


_name = st.text(
    st.characters(min_codepoint=33, max_codepoint=0x2FFF, exclude_categories=("Cs", "Zs", "Cc")),
    min_size=1,
    max_size=12,
)
_key = st.from_regex(r"[A-Za-z0-9_.\-]{1,12}", fullmatch=True)


def _ordered(t: tuple[Any, Any]) -> bool:
    lo, hi = t
    return (lo, hi) != (None, None) and (lo is None or hi is None or lo <= hi)


_int_range: st.SearchStrategy[tuple[int | None, int | None]] = st.tuples(
    st.none() | st.integers(0, 10**6), st.none() | st.integers(0, 10**6)
).filter(_ordered)
_dec = st.decimals(min_value=0, max_value=1000, places=4, allow_nan=False, allow_infinity=False)
_dec_range: st.SearchStrategy[tuple[Decimal | None, Decimal | None]] = st.tuples(
    st.none() | _dec, st.none() | _dec
).filter(_ordered)
_attr = st.builds(
    lambda scope, key, value: AttrPredicate(
        scope=scope, key=key, op="exists" if value is None else "eq", value=value
    ),
    st.sampled_from(["root", "resource"]),
    _key,
    st.none() | _name,
)


@st.composite
def _filters(draw: st.DrawFn) -> RunFilter:
    dur = draw(st.none() | _int_range)
    tok = draw(st.none() | _int_range)
    cost = draw(st.none() | _dec_range)
    return RunFilter(
        project_ids=draw(st.lists(_name, max_size=3)),
        sources=draw(st.lists(_name, max_size=3)),
        time_from=draw(st.none() | st.sampled_from(["now-1h", "now-7d", "2026-10-30T12:00:00Z"])),
        time_to=draw(st.none() | st.sampled_from(["now", "2026-10-31T00:00:00+02:00"])),
        statuses=draw(st.lists(st.sampled_from(["in_progress", "ok", "error", "incomplete"]))),
        is_genai=draw(st.none() | st.booleans()),
        models=draw(st.lists(_name, max_size=3)),
        tools=draw(st.lists(_name, max_size=3)),
        duration_ms=None if dur is None else Range[int](min=dur[0], max=dur[1]),
        tokens=None if tok is None else Range[int](min=tok[0], max=tok[1]),
        cost_usd=None if cost is None else Range[Decimal](min=cost[0], max=cost[1]),
        include_unpriced_in_cost_range=draw(st.booleans()),
        attributes=draw(st.lists(_attr, max_size=3)),
        text=draw(st.none() | _name),
        session_id=draw(st.none() | st.just(SID)),
    )


@given(
    f=_filters(),
    sort=st.sampled_from(["start_desc", "duration_asc", "cost_desc"]),
    cursor=st.none() | st.just("a.b"),
)
def test_parse_serialize_round_trip_req_102b(f: RunFilter, sort: str, cursor: str | None) -> None:
    """REQ-102.b: every filter set survives the URL, and the string form is canonical."""
    text = runs_query_string(f, sort, cursor)  # type: ignore[arg-type]
    q = parse_runs_query(text)
    assert q.filter == f
    assert (q.sort, q.cursor) == (sort, cursor)
    assert runs_query_string(q.filter, q.sort, q.cursor) == text


# ── scope ────────────────────────────────────────────────────────────────────────────────────


class _Projects:
    def __init__(self, *slugs: str) -> None:
        self.items = [
            r.Project(
                id=f"id-{s}",
                slug=s,
                name=s,
                content_storage=True,
                retention_max_age_s=0,
                request_rate=1,
                request_burst=1,
                span_rate=1,
                span_burst=1,
                settings_version=1,
                created_at_ns=0,
                updated_at_ns=0,
                updated_by=None,
            )
            for s in slugs
        ]

    async def list_projects(self) -> list[r.Project]:
        return self.items


ADMIN = Viewer("u", "U", is_admin=True)
LIMITED = Viewer("v", "V", is_admin=False, allowed_project_ids=frozenset({"id-a"}))


async def test_resolve_projects_req_191c() -> None:
    store = _Projects("a", "b")
    assert await resolve_projects(store, ADMIN) == {"id-a", "id-b"}
    assert await resolve_projects(store, LIMITED) == {"id-a"}
    assert await resolve_projects(store, ADMIN, ["b", "id-a"]) == {"id-a", "id-b"}
    with pytest.raises(Forbidden):
        await resolve_projects(store, LIMITED, ["b"])
    with pytest.raises(Forbidden):  # an unknown Project looks the same as a forbidden one
        await resolve_projects(store, LIMITED, ["zzz"])
    with pytest.raises(ValidationError) as exc:
        await resolve_projects(store, ADMIN, ["zzz"])
    assert exc.value.field == "project"


def test_source_labels() -> None:
    assert source_label(r.SourceKey("svc", "ns")) == "ns/svc"
    assert parse_source_label("ns/svc") == r.SourceKey("svc", "ns")
    assert parse_source_label("svc") == r.SourceKey("svc")
    known = [r.SourceKey("a/b")]  # a name with a slash and no namespace
    assert parse_source_label("a/b", known) == r.SourceKey("a/b")


def test_run_query_translation() -> None:
    f = parse_runs_query(
        "source=ns/svc&from=now-1h&dur=1..2&tok=3..&cost=..0.5&attr=resource:k=v&status=ok"
    ).filter
    q = run_query(f, frozenset({"p"}), NOW, sort="tokens_desc", limit=10)
    assert q.project_ids == {"p"}
    assert q.sources == (r.SourceKey("svc", "ns"),)
    assert q.time_range == r.TimeRange(NOW - 3600 * 10**9, MAX_NS)
    assert q.duration_ns == r.Range(1_000_000, 2_000_000)
    assert q.tokens == r.Range(3, None)
    assert q.cost_nusd == r.Range(None, 500_000_000)
    assert q.attributes == (r.AttrPredicate(key="k", scope="resource", op="eq", value="v"),)
    assert q.statuses == ("ok",)
    assert (q.sort, q.limit) == ("tokens_desc", 10)
    for bad in (0, 201):
        with pytest.raises(ValidationError) as exc:
            run_query(f, frozenset({"p"}), NOW, limit=bad)
        assert exc.value.field == "limit"
