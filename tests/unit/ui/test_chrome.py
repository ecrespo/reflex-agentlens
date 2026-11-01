"""Shared chrome URL state, selectors, tokens and snippet (plan tasks P6-01.1, P6-01.2, P6-02.1;
REQ-130.a/b/e, 150.a, 191.c)."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import reflex_silverpoint_react as sp  # type: ignore[import-untyped]
from hypothesis import given
from hypothesis import strategies as st

from reflex_agentlens.core.config import load_config
from reflex_agentlens.query.dto import (
    ProjectRef,
    RunStatus,
    SourceRef,
    SourceView,
    SpanKind,
    Viewer,
)
from reflex_agentlens.views.chrome import (
    DEFAULT_RANGE,
    RANGES,
    Chrome,
    decode_chrome,
    encode_chrome,
    project_options,
    source_options,
)
from reflex_agentlens.views.snippets import KEY_PLACEHOLDER, quickstart_snippet
from reflex_agentlens.views.tokens import KIND_COLORS, PALETTE, STATUS_COLORS


def test_every_kind_and_status_has_a_readable_colour_req_130e_150a() -> None:
    assert set(KIND_COLORS) == set(get_args(SpanKind))
    assert set(get_args(RunStatus)) <= set(STATUS_COLORS)
    assert sp.check_palette(PALETTE) == []
    substrate = sp.PALETTES[PALETTE].substrate
    for color in [*KIND_COLORS.values(), *STATUS_COLORS.values()]:
        assert sp.contrast_ratio(color, substrate) >= 4.5, color


def chromes() -> st.SearchStrategy[Chrome]:
    label = st.text(st.characters(codec="ascii", categories=["L", "N"]), min_size=1, max_size=12)
    named = st.sampled_from(list(RANGES)).map(lambda k: (k, RANGES[k], None))
    custom = st.tuples(
        st.just("custom"),
        st.integers(1, 999).map(lambda n: f"now-{n}m"),
        st.one_of(st.none(), st.just("now"), st.just("2026-10-30T12:00:00Z")),
    ).filter(lambda t: t[1] not in RANGES.values() or t[2] is not None)
    return st.builds(
        lambda project, sources, rng, live: Chrome(
            project, tuple(sources), rng[0], rng[1], rng[2], live
        ),
        st.one_of(st.none(), label),
        st.lists(label, max_size=4, unique=True),
        st.one_of(named, custom),
        st.booleans(),
    )


@given(chromes())
def test_url_round_trip_req_130a(chrome: Chrome) -> None:
    pairs = encode_chrome(chrome)
    params: dict[str, list[str]] = {}
    for key, value in pairs:
        params.setdefault(key, []).append(value)
    assert decode_chrome(params) == chrome


def test_defaults_are_omitted_and_invalid_values_fall_back() -> None:
    assert encode_chrome(Chrome()) == []
    chrome = decode_chrome({"from": "yesterday", "to": "x", "live": "0", "project": "\x00"})
    assert (chrome.range, chrome.from_, chrome.to, chrome.live) == (
        DEFAULT_RANGE, RANGES[DEFAULT_RANGE], None, False,
    )  # fmt: skip
    assert chrome.project is None
    assert decode_chrome({"source": ["a", "a", " ", "b"]}).sources == ("a", "b")
    assert Chrome().with_range("7d").from_ == "now-7d"
    assert Chrome().with_range("bogus") == Chrome()


def test_project_options_respect_the_viewer_req_191c() -> None:
    projects = [
        ProjectRef(id="p1", slug="one", name="Zeta"),
        ProjectRef(id="p2", slug="two", name="alpha"),
    ]
    everyone = Viewer("a", "A", is_admin=True)
    restricted = Viewer("b", "B", is_admin=False, allowed_project_ids=frozenset({"p1"}))
    assert [o.label for o in project_options(everyone, projects)] == ["alpha", "Zeta"]
    assert [o.value for o in project_options(restricted, projects)] == ["p1"]


def test_source_options_are_unique_labels() -> None:
    def view(pid: str, name: str, ns: str = "") -> SourceView:
        ref = SourceRef(service_name=name, service_namespace=ns)
        return SourceView(project_id=pid, source=ref, first_seen_ns=0, last_seen_ns=0)

    sources = [view("p1", "api"), view("p2", "api"), view("p1", "worker", "jobs")]
    assert [o.value for o in source_options(sources)] == ["api", "jobs/worker"]


def test_quickstart_snippet_uses_a_placeholder_key_req_130b(tmp_path: Path) -> None:
    cfg = load_config(env={"HOME": str(tmp_path)}, cwd=tmp_path)
    text = quickstart_snippet(cfg)
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:8000"' in text
    assert KEY_PLACEHOLDER in text
    public = load_config(
        env={"HOME": str(tmp_path)}, cwd=tmp_path,
        cli_overrides={"server.public_url": "https://obs.example/"},
    )  # fmt: skip
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT="https://obs.example"' in quickstart_snippet(public)
