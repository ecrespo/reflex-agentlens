"""Public embedding API and ``mount()`` (plan tasks P6-03.1, P6-03.2; REQ-190.a/b/c/d, 191, API §7).

The import audit follows the S1 rule (Tech Design §11.1, P0-05.3): importing must open no socket
and start no subprocess, and may write files only below the current directory (Reflex's own
asset links); it runs in a fresh interpreter with an empty working directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import reflex as rx
from reflex_base.registry import RegistrationContext

from reflex_agentlens.pages import ALL_PAGES, PAGES, ROUTES
from reflex_agentlens.pages._base import AgentlensState, ui_event
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views import registry as reg_module

AUDIT = textwrap.dedent(
    """
    import json, os, sys
    root = os.path.realpath(os.getcwd())
    events = []

    def hook(event, args):
        if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
            events.append([event, str(args[1:2])])
        elif event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"):
            events.append([event, str(args[:1])])
        elif event == "open" and len(args) > 1 and isinstance(args[1], str) and any(
            m in args[1] for m in "wax+"
        ):
            path = os.path.realpath(str(args[0]))
            if not path.startswith(root) and not path.endswith(".pyc"):
                events.append([event, path])
        elif event in ("os.symlink", "os.rename"):
            # (src, dst): the entry written is dst; never resolve it (it may be a link)
            dst = os.path.abspath(str(args[1]))
            path = os.path.join(os.path.realpath(os.path.dirname(dst)), os.path.basename(dst))
            if not path.startswith(root):
                events.append([event, path])
        elif event in ("os.mkdir", "os.remove"):
            path = os.path.realpath(str(args[0]))
            if not path.startswith(root) and "__pycache__" not in path:
                events.append([event, path])

    sys.addaudithook(hook)
    import reflex_agentlens
    reflex_agentlens.configure(None)
    _ = reflex_agentlens.Viewer, reflex_agentlens.AgentlensConfig, reflex_agentlens.AuthHook
    core_only = "reflex" not in sys.modules
    import reflex_agentlens.pages
    _ = reflex_agentlens.mount
    print(json.dumps({"events": events, "core_only": core_only}))
    """
)


def test_import_performs_no_io_req_190c(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REFLEX_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-c", AUDIT], cwd=tmp_path, env=env, capture_output=True, text=True,
        check=True, timeout=120,
    )  # fmt: skip
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["events"] == []
    assert report["core_only"]  # import reflex_agentlens + configure/Viewer: no Reflex


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    reg_module.reset()
    try:
        yield
    finally:
        reg_module.reset()


def new_app() -> rx.App:
    """A fresh ``rx.App``: Reflex 0.10 binds one App per registration context."""
    RegistrationContext.set(RegistrationContext.ensure_context().fork())
    return rx.App()


@pytest.fixture(autouse=True)
def isolated_context() -> Iterator[None]:
    token = RegistrationContext.set(RegistrationContext.ensure_context().fork())
    try:
        yield
    finally:
        RegistrationContext.reset(token)


async def allow(state: Any) -> Viewer | None:
    return Viewer("ana", "Ana", is_admin=True)


def routes_of(app: rx.App) -> set[str]:
    return set(app._unevaluated_pages)


def test_mount_registers_every_page_under_the_prefix_req_190b(fresh_registry: None) -> None:
    from reflex_agentlens.app.mount import mount

    app = new_app()
    mount(app, auth=allow, prefix="/obs/", login_route="/login")
    assert routes_of(app) == {
        "obs", "obs/runs", "obs/trace/[trace_id]", "obs/sessions", "obs/sessions/[session_id]",
        "obs/settings",
    }  # fmt: skip
    reg = reg_module.registry()
    assert (reg.auth, reg.login_route, reg.prefix) == (allow, "/login", "/obs")
    assert any(getattr(t, "__name__", "") == "agentlens_lifespan" for t in app._lifespan_tasks)
    assert app.api_transformer is None  # no ingest, no health: Reflex's backend untouched
    with pytest.raises(RuntimeError, match="already"):
        mount(app, auth=allow)


def test_mount_subset_health_and_validation(fresh_registry: None) -> None:
    from reflex_agentlens.app.mount import mount

    app = new_app()
    mount(app, auth=allow, pages={"runs", "trace"}, health=True)
    assert routes_of(app) == {"agentlens/runs", "agentlens/trace/[trace_id]"}
    assert callable(app.api_transformer)
    for bad in ({"pages": {"nope"}}, {"prefix": "agentlens"}, {"grpc": True}):
        with pytest.raises(ValueError):  # noqa: PT011 - the message varies per case
            mount(new_app(), auth=allow, **bad)


def test_routes_match_the_url_contract() -> None:
    assert set(ROUTES) == ALL_PAGES == set(PAGES)
    assert ROUTES["trace"] == "/trace/[trace_id]"
    for page in PAGES.values():
        assert page.on_load[0] == AgentlensState.check_auth  # every page asks the hook first


def test_states_are_namespaced_req_190d() -> None:
    assert AgentlensState.get_name().startswith("reflex_agentlens___")


def test_pages_render() -> None:
    for page in PAGES.values():
        assert page.component().render()


class Probe(AgentlensState):
    seen: str = ""

    @ui_event
    async def load(self, viewer: Viewer, project: str) -> None:
        self.seen = f"{viewer.id}:{project}"


def test_ui_event_hides_the_viewer_argument() -> None:
    handler = Probe.event_handlers["load"]
    import inspect

    assert list(inspect.signature(handler.fn).parameters) == ["self", "project"]


def test_chrome_bar_renders_with_every_range_req_130a() -> None:
    from reflex_agentlens.components.chrome import ChromeState, chrome_bar

    rendered = str(chrome_bar().render())
    for label in ("1 h", "24 h", "7 d", "30 d"):
        assert label in rendered
    assert "load_chrome" in ChromeState.event_handlers


def test_runs_and_trace_pages_compile_to_js(fresh_registry: None) -> None:
    """P6-04.2, P6-05.2: the pages evaluate inside a real ``rx.App`` and compile to JS."""
    from reflex.compiler import compiler

    from reflex_agentlens.app.mount import mount

    app = new_app()
    mount(app, auth=allow, pages={"runs", "trace"})
    for route in ("agentlens/runs", "agentlens/trace/[trace_id]"):
        app._compile_page(route)
        code = compiler._compile_page(app._pages[route], route)
        assert "agentlens" in code
    runs_js = compiler._compile_page(app._pages["agentlens/runs"], "agentlens/runs")
    for text in ("Saved filters", "new runs", "Next page", "Include unpriced"):
        assert text in runs_js
    trace_js = compiler._compile_page(
        app._pages["agentlens/trace/[trace_id]"], "agentlens/trace/[trace_id]"
    )
    for text in (
        "Load more spans",
        "several Projects",
        "Timeline",
        "Raw attributes",
        "Exit replay",
        "Replay speed",
    ):
        assert text in trace_js
    # REQ-324: strict Mermaid, no icon packs, no Font Awesome (P6-07.2)
    for prop in ('securityLevel:"strict"', "fontAwesome:false", "iconPacks:[]", "htmlLabels:false"):
        assert prop in trace_js
