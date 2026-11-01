"""Event guard and auth-hook resolution (plan tasks P6-01.2, P6-03.2; REQ-191.a/b, §5.5).

The A-12 tests come from Delta ``changes/2026-11-p6-ui-prep/`` (PRD v1.2 REQ-191.a).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from reflex_agentlens.core.errors import Forbidden, NotFound, StorageUnavailable, ValidationError
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views.guard import (
    VIEWER_TTL_S,
    ViewerCache,
    classify,
    guarded,
    resolve_viewer,
)

ANA = Viewer("ana", "Ana", is_admin=False)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (StorageUnavailable("db down"), "storage"),
        (Forbidden("project"), "forbidden"),
        (NotFound("run"), "not_found"),
        (ValidationError("from", "bad time"), "invalid"),
        (RuntimeError("boom"), "error"),
    ],
)
def test_errors_map_to_ui_states_tech_design_5_5(exc: Exception, kind: str) -> None:
    assert classify(exc).kind == kind


def test_unexpected_errors_log_the_class_only_req_323(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    error = classify(KeyError("secret-prompt-text"))
    assert error.kind == "error"
    assert "secret-prompt-text" not in error.message
    assert "KeyError" in caplog.text
    assert "secret-prompt-text" not in caplog.text


def test_guarded_returns_result_or_state() -> None:
    async def ok() -> int:
        return 7

    async def bad() -> int:
        raise Forbidden("x")

    assert asyncio.run(guarded(ok)) == (7, None)
    result, error = asyncio.run(guarded(bad))
    assert result is None
    assert error is not None
    assert error.kind == "forbidden"


class Hook:
    def __init__(self, viewer: Viewer | None = ANA) -> None:
        self.viewer = viewer
        self.calls = 0

    async def __call__(self, state: Any) -> Viewer | None:
        self.calls += 1
        return self.viewer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def resolve(hook: Hook, cache: ViewerCache, token: str, *, page_load: bool) -> Viewer | None:
    return asyncio.run(resolve_viewer(hook, object(), token, page_load=page_load, cache=cache))


def test_page_loads_always_call_the_hook_req_191a() -> None:
    hook, cache = Hook(), ViewerCache(clock=Clock())
    assert resolve(hook, cache, "t1", page_load=True) == ANA
    hook.viewer = None  # the host logged the user out
    assert resolve(hook, cache, "t1", page_load=True) is None  # within 30 s: still asked
    assert hook.calls == 2


def test_events_reuse_the_viewer_for_at_most_30_s_req_191a() -> None:
    clock = Clock()
    hook, cache = Hook(), ViewerCache(clock=clock)
    resolve(hook, cache, "t1", page_load=True)
    clock.now += VIEWER_TTL_S - 0.1
    assert resolve(hook, cache, "t1", page_load=False) == ANA
    assert hook.calls == 1  # reused
    clock.now += 0.2  # 30 s since the hook answered
    hook.viewer = None
    assert resolve(hook, cache, "t1", page_load=False) is None
    assert hook.calls == 2


def test_cached_viewers_never_cross_client_tokens_req_191a() -> None:
    hook, cache = Hook(), ViewerCache(clock=Clock())
    resolve(hook, cache, "t1", page_load=True)
    hook.viewer = None
    assert resolve(hook, cache, "t2", page_load=False) is None  # another client asks the hook
    assert resolve(hook, cache, "", page_load=False) is None  # no token: never cached
    assert resolve(hook, cache, "", page_load=False) is None
    assert hook.calls == 4  # t1 page load, t2 event, two token-less events


def test_cache_is_bounded() -> None:
    cache = ViewerCache(clock=Clock(), max_clients=2)
    for token in ("a", "b", "c"):
        cache.put(token, ANA)
    assert cache.get("a") is not ANA  # evicted
    assert cache.get("c") is ANA


def test_hook_must_return_a_viewer() -> None:
    async def bad(state: Any) -> Any:
        return {"id": "ana"}

    with pytest.raises(TypeError, match="Viewer"):
        asyncio.run(resolve_viewer(bad, object(), "t", page_load=True, cache=ViewerCache()))
