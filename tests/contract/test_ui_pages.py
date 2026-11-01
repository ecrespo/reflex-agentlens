"""Runs and Trace page events against both backends (plan tasks P6-04.2, P6-05.2).

The page states run their real event bodies (``ui_event`` included) over a real
``QueryService`` / ``AdminService``; only the URL is supplied by the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from reflex_base.utils.format import json_dumps

from reflex_agentlens.core.config import load_config
from reflex_agentlens.pages.runs_page import RunsState
from reflex_agentlens.pages.trace_page import TraceState
from reflex_agentlens.query import dto
from reflex_agentlens.query.admin import AdminService
from reflex_agentlens.query.service import QueryService
from reflex_agentlens.store.records import NormalizedBatch
from reflex_agentlens.views import registry as reg_module
from reflex_agentlens.views.trace import TraceUrl
from tests.contract.conftest import ContractDB
from tests.contract.dataset import dataset

RUNS = dataset(7, 120)
KEY = b"u" * 32


async def allow(state: Any) -> dto.Viewer | None:
    return dto.Viewer("ana", "Ana", is_admin=True)


@pytest.fixture
def wired() -> Iterator[None]:
    reg_module.reset()
    try:
        yield
    finally:
        reg_module.reset()


async def setup(db: ContractDB, tmp_path: Path) -> str:
    project = await db.make_project("shop")
    spans = tuple(sp for run in RUNS for sp in run)
    await db.require_backend().write_batch(NormalizedBatch(project, spans))
    backend = db.require_backend()
    cfg = load_config(env={}, cwd=tmp_path, cli_overrides={})
    reg = reg_module.registry()
    reg.auth, reg.prefix = allow, "/obs"
    reg.runtime = SimpleNamespace(  # type: ignore[assignment]
        query=QueryService(backend, KEY, clock_ns=db.clock),
        admin=AdminService(backend, cfg, clock_ns=db.clock),
    )
    return project


def runs_state(monkeypatch: pytest.MonkeyPatch, query: str) -> RunsState:
    monkeypatch.setattr(RunsState, "_url_query", lambda self: query)
    return RunsState()


@pytest.mark.usefixtures("wired")
async def test_runs_page_lists_saves_and_validates(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await setup(db, tmp_path)
    state = runs_state(monkeypatch, "project=shop&live=0")
    assert await state.load_runs() is None  # live=0: no subscription
    assert state.error_kind == ""
    assert state.rows
    assert len(state.rows) <= len(RUNS)
    assert all(r.href.startswith("/obs/trace/") for r in state.rows)
    assert state.project_id == project  # the slug resolves to the Project's ID
    assert state.query == "project=shop"
    sent = json_dumps(state.dict())  # what the browser receives: rows as plain objects
    assert state.rows[0].trace_id in sent

    await state.save_filter({"name": "everything"})
    assert [f["name"] for f in state.saved] == ["everything"]
    assert state.saved[0]["href"] == "/obs/runs?project=shop"
    await state.delete_filter(state.saved[0]["id"])
    assert state.saved == []

    await state.apply_filters({"dur": "5..1"})
    assert state.error_kind == "invalid"
    assert "dur" in state.error_message


@pytest.mark.usefixtures("wired")
async def test_runs_page_starts_live_updates_on_the_first_page(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup(db, tmp_path)
    state = runs_state(monkeypatch, "")
    assert await state.load_runs() == RunsState.watch
    assert state.live_first
    assert state.query == ""


@pytest.mark.usefixtures("wired")
async def test_trace_page_loads_toggles_and_reports_not_found(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup(db, tmp_path)
    trace_id = RUNS[0][0].trace_id.hex()
    monkeypatch.setattr(TraceState, "_url", lambda self: (trace_id, TraceUrl(tab="cost")))
    state = TraceState()
    await state.load_trace()
    assert state.error_kind == ""
    assert state.found
    assert state.rows
    assert state.tab in state.tabs
    assert any(r["label"] == "Total tokens" for r in state.totals)
    assert state._serialize()  # backend-only vars (the loaded tree) survive a state store
    assert "_spans" not in json_dumps(state.dict())

    parent = next((r for r in state.rows if r.has_children), None)
    if parent is not None:
        before = len(state.rows)
        state.toggle_row(parent.span_id, parent.depth, parent.collapsed)  # type: ignore[operator]
        assert len(state.rows) != before

    monkeypatch.setattr(TraceState, "_url", lambda self: ("ef" * 16, TraceUrl()))
    await state.load_trace()
    assert (state.found, state.error_kind) == (False, "not_found")


@pytest.mark.usefixtures("wired")
async def test_live_steps_ask_the_hook_again_after_the_cache_a12(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live tasks never keep the identity they started with: once the cached answer has
    expired a revoked viewer stops the task and sees "not authorized"."""
    await setup(db, tmp_path)
    allowed = [True]

    async def revocable(state: Any) -> dto.Viewer | None:
        return dto.Viewer("ana", "Ana", is_admin=False) if allowed[0] else None

    reg = reg_module.registry()
    reg.auth = revocable
    state = runs_state(monkeypatch, "")
    state.authorized = True
    assert await state._live_viewer() is not None
    allowed[0] = False
    reg.viewers.forget(state._token())  # what the 30 s expiry does
    assert await state._live_viewer() is None
    assert state.authorized is False


def _llm_span(runs: list[Any]) -> tuple[str, str]:
    for run in runs:
        for sp in run:
            if sp.llm_call is not None:
                return sp.trace_id.hex(), sp.span_id.hex()
    raise AssertionError("dataset has no LLM span")


@pytest.mark.usefixtures("wired")
async def test_drawer_graph_and_replay(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P6-06.2, P6-07.2: selecting a span opens the drawer (raw tab built lazily), a graph node
    selects its span, replay steps in start order; each rewrites the URL in place."""
    await setup(db, tmp_path)
    trace_id, span_id = _llm_span(RUNS)
    monkeypatch.setattr(
        TraceState, "_url", lambda self: (trace_id, TraceUrl(span=span_id, project="shop"))
    )
    state = TraceState()
    await state.load_trace()
    assert state.drawer is not None  # ``span=`` in the URL opens the drawer on load
    assert state.drawer.span_id == span_id
    assert state.drawer.llm
    assert state.raw_sections == []  # not built until the raw tab is opened
    state.pick_drawer_tab("raw")  # type: ignore[operator]
    assert [s.title for s in state.raw_sections][:2] == ["Attributes", "Resource"]

    spec = state.close_drawer()  # type: ignore[operator]
    assert (state.drawer, state.selected) == (None, "")  # type: ignore[comparison-overlap]
    assert f"/obs/trace/{trace_id}?project=shop" in str(spec)

    spec = await state.select_span("not-a-span-id")
    assert spec is None
    spec = await state.select_span(span_id)
    assert state.selected == span_id
    assert f"span={span_id}" in str(spec)

    assert state.chart.startswith("flowchart LR")  # the dataset's agent Runs have a graph
    node = next(n for n, spans in state._node_spans.items() if spans)
    assert state.on_node_click({"node_id": node}) is not None  # type: ignore[operator]
    assert state.on_node_click({"node_id": "nope"}) is None  # type: ignore[operator]

    state.start_replay()  # type: ignore[operator]
    first = state.replay
    assert first
    spec = state.step_replay(1)  # type: ignore[operator]
    assert state.replay != first or len(state.future) == 0
    assert "replay=" in str(spec)
    state.set_speed("step")  # type: ignore[operator]
    assert state.toggle_play() is None  # type: ignore[operator]  # step mode never auto-plays
    state.set_speed("5")  # type: ignore[operator]
    assert state.toggle_play() == TraceState.play  # type: ignore[operator]
    state.exit_replay()  # type: ignore[operator]
    assert (state.replay, state.future, state.playing) == ("", [], False)


def _set_url(monkeypatch: pytest.MonkeyPatch, cls: type, url: str, **params: str) -> None:
    router = SimpleNamespace(url=url, page=SimpleNamespace(params=params, path=url.split("?")[0]),
                             session=SimpleNamespace(client_token="t1"))  # noqa: S106  # fmt: skip
    monkeypatch.setattr(cls, "router", router, raising=False)


@pytest.mark.usefixtures("wired")
async def test_sessions_pages_req_160(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reflex_agentlens.pages.session_page import SessionState
    from reflex_agentlens.pages.sessions_page import SessionsState

    await setup(db, tmp_path)
    _set_url(monkeypatch, SessionsState, "http://h/obs/sessions?project=shop&from=now-3650d")
    sessions = SessionsState()
    await sessions.load_sessions()
    assert sessions.error_kind == ""
    assert sessions.rows
    assert sessions.rows[0].href.startswith("/obs/sessions/")
    spec = sessions.pick_sort("runs_desc")  # type: ignore[operator]
    assert "sort=runs_desc" in str(spec)

    session_id = sessions.rows[0].session_id
    _set_url(monkeypatch, SessionState, f"http://h/obs/sessions/{session_id}",
             session_id=session_id)  # fmt: skip
    session = SessionState()
    await session.load_session()
    assert session.error_kind == ""
    assert session.found
    assert session.rows
    tokens = [p["tokens"] for p in session.series]
    assert tokens == sorted(tokens)  # cumulative

    _set_url(monkeypatch, SessionState, "http://h/obs/sessions/nope", session_id="nope")
    missing = SessionState()
    await missing.load_session()
    assert (missing.found, missing.error_kind) == (False, "not_found")


@pytest.mark.usefixtures("wired")
async def test_dashboard_loads_and_clicks_through_req_141(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reflex_agentlens.pages.dashboard_page import DashboardState

    await setup(db, tmp_path)
    _set_url(monkeypatch, DashboardState, "http://h/obs?project=shop&from=now-3650d&live=0")
    state = DashboardState()
    assert await state.load_dashboard() is None  # live=0: no refresh task
    assert state.error_kind == ""
    assert state.failed == []
    assert state.has_data
    assert len(state.kpis) == 6
    assert state.runs_by_status
    assert state.tokens_by_model
    row = state.tokens_by_model[0]
    spec = state.select("model", {"seriesKey": "input", "datum": row})  # type: ignore[operator]
    assert "/obs/runs?" in str(spec)
    assert "model=" in str(spec)
    assert state.select("model", {"datum": {"link": "//evil"}}) is None  # type: ignore[operator]

    _set_url(monkeypatch, DashboardState, "http://h/obs?project=shop&from=now-3650d")
    assert await DashboardState().load_dashboard() == DashboardState.watch


def _settings(monkeypatch: pytest.MonkeyPatch, query: str = "") -> Any:
    from reflex_agentlens.pages.settings_page import SettingsState

    _set_url(monkeypatch, SettingsState, f"http://h/obs/settings?{query}")
    return SettingsState()


@pytest.mark.usefixtures("wired")
async def test_settings_projects_and_keys_req_170(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P6-10.1: create a Project and a key; the plaintext leaves the state when its dialog
    closes and never survives a reload; revoke; delete only with the exact name."""
    await setup(db, tmp_path)
    state = _settings(monkeypatch, "project=shop")
    await state.load_settings()
    assert (state.can_view, state.can_mutate, state.error_kind) == (True, True, "")
    assert [p.slug for p in state.projects] == ["shop"]

    await state.create_project({"name": "Billing", "slug": "billing"})
    assert state.action_error == ""
    assert state.project_name == "Billing"  # the new Project becomes the selected one
    await state.create_project({"name": "Billing", "slug": "billing"})
    assert "already exists" in state.action_error

    await state.create_key({"label": "ci"})
    plaintext = state.reveal.plaintext
    assert plaintext
    assert plaintext in state.reveal.snippet
    assert [k.label for k in state.keys] == ["ci"]
    assert plaintext not in json_dumps(state.keys)  # rows carry the prefix only
    state.close_reveal()
    assert plaintext not in json_dumps(state.dict())

    await state.create_key({"label": "again"})
    assert state.reveal.plaintext
    from reflex_agentlens.pages.settings_page import SettingsState

    _set_url(monkeypatch, SettingsState, "http://h/obs/settings?project=billing")
    await state.load_settings()  # a reload never shows a plaintext again
    assert state.project_name == "Billing"
    assert state.reveal.plaintext == ""

    await state.revoke_key(state.keys[0].key_id)
    assert state.action_error == ""
    assert not state.keys[-1].active

    await state.delete_project({"confirm": "billing"})  # wrong case: refused
    assert "exactly" in state.action_error
    await state.delete_project({"confirm": "Billing"})
    assert state.action_error == ""
    assert [p.slug for p in state.projects] == ["shop"]


@pytest.mark.usefixtures("wired")
async def test_settings_require_admin_and_demo_is_read_only_req_174_231b(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup(db, tmp_path)
    reg = reg_module.registry()

    async def viewer(state: Any) -> dto.Viewer | None:
        return dto.Viewer("bo", "Bo", is_admin=False)

    reg.auth = viewer
    state = _settings(monkeypatch)
    await state.load_settings()
    assert (state.can_view, state.can_mutate) == (False, False)
    assert state.projects == []  # nothing is read for a non-admin viewer
    await state.create_project({"name": "Sneaky", "slug": "sneaky"})
    assert state.action_error == "Settings require admin rights."
    assert state.error_kind == ""  # refused inline: the page stays in place

    reg.auth, reg.demo = allow, True
    reg.viewers.forget("t1")  # the hook's last answer is cached for 30 s (A-12)
    state = _settings(monkeypatch)
    await state.load_settings()
    assert (state.can_view, state.can_mutate) == (True, False)
    assert "demo" in state.access_notice
    await state.create_key({"label": "x"})
    assert state.reveal.plaintext == ""
    assert "demo" in state.action_error


@pytest.mark.usefixtures("wired")
async def test_settings_pricing_editor_and_csv_round_trip_req_171(
    db: ContractDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P6-10.2: edit and save a new version, validation stops a bad save, the exported CSV
    imports back as a version with the same entries, older versions are read-only."""
    from reflex_agentlens.views.pricing import PriceRow

    await setup(db, tmp_path)
    state = _settings(monkeypatch, "tab=pricing&project=shop")
    await state.load_settings()
    assert state.tab == "pricing"
    assert state.error_kind == ""
    first = state.price_current
    assert state.price_viewing == first
    assert state.unpriced  # the dataset's models have no price yet

    model = state.unpriced[0].model
    state.add_unpriced(state.unpriced[0].provider, model)
    index = len(state.price_rows) - 1
    assert state.price_rows[index].pattern == model
    state.set_price_cell(index, "input", "abc")
    await state.save_pricing({"note": ""})
    assert state.price_current == first  # nothing saved
    assert state.price_errors == [
        f"Row {index + 1}: Input $/1M must be a decimal number such as 2.50.",
        f"Row {index + 1}: Output $/1M is required.",
    ]
    state.set_price_cell(index, "input", "1.5")
    state.set_price_cell(index, "output", "3")
    state.set_price_cell(index, "nope", "3")  # unknown columns are ignored
    await state.save_pricing({"note": "dataset model"})
    assert (state.action_error, state.price_errors) == ("", [])
    assert state.price_current == first + 1
    assert PriceRow(pattern=model, provider=state.price_rows[index].provider, input="1.5",
                    output="3") in state.price_rows  # fmt: skip
    assert model in {u.model for u in state.unpriced}  # stored calls keep their cost

    spec = await state.export_pricing()
    _, admin = reg_module.services()
    exported = await admin.export_pricing_csv(dto.Viewer("ana", "Ana", is_admin=True))
    assert spec is not None
    assert "agentlens-pricing-v" in str(spec)
    saved_rows = list(state.price_rows)
    await state.import_pricing({"csv": exported})
    assert state.action_error == ""
    assert state.price_current == first + 2
    assert state.price_rows == saved_rows  # the round trip keeps every entry
    await state.import_pricing({"csv": "not,a,pricing,csv\n"})
    assert state.action_error.startswith("Invalid ")
    assert state.price_current == first + 2

    await state.pick_price_version(str(first + 1))  # the version saved from the editor
    assert state.price_viewing == first + 1
    await state.save_pricing({})
    assert "current version" in state.action_error

    await state.recompute({"from": "now-3650d"})
    assert state.action_error == ""
    assert "LLM calls re-priced" in state.action_done
    assert model not in {u.model for u in state.unpriced}
