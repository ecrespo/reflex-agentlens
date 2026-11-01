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
