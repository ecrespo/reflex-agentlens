"""Settings view model: access, URL, Projects and API keys (plan task P6-10.1; REQ-170, 174,
231.b)."""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from reflex_agentlens.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    StorageUnavailable,
    ValidationError,
)
from reflex_agentlens.query.dto import ApiKeyView, CreatedApiKey, Project
from reflex_agentlens.views import settings as vm
from tests.unit.ui.factories import T0

PLAIN = "al_test_0123456789abcdef"


def project(pid: str, slug: str, **kw: object) -> Project:
    base: dict[str, object] = {
        "id": pid, "slug": slug, "name": slug.title(), "content_storage": True,
        "retention_days": 30, "request_rate": 10, "request_burst": 20, "span_rate": 100,
        "span_burst": 200, "settings_version": 1, "created_at_ns": T0, "updated_at_ns": T0,
        "updated_by": None,
    }  # fmt: skip
    base.update(kw)
    return Project(**base)  # type: ignore[arg-type]


def key(kid: str, created: int, revoked_at: int | None = None) -> ApiKeyView:
    return ApiKeyView(
        key_id=kid, prefix=f"al_{kid}", label="", created_at_ns=created, created_by=None,
        last_used_at_ns=None, revoked_at_ns=revoked_at, revoked=revoked_at is not None,
    )  # fmt: skip


def test_access_req_174a_231b() -> None:
    assert vm.access(is_admin=False, demo=False) == vm.Access(False, False, vm.REQUIRES_ADMIN)
    assert vm.access(is_admin=False, demo=True).visible is False  # demo never widens access
    demo = vm.access(is_admin=True, demo=True)
    assert (demo.visible, demo.can_mutate, demo.notice) == (True, False, vm.DEMO_NOTICE)
    assert vm.access(is_admin=True, demo=False) == vm.Access(True, True, "")


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (Forbidden("requires admin"), vm.REQUIRES_ADMIN),
        (Forbidden("demo mode"), vm.DEMO_NOTICE),
        (Forbidden("requires access to every Project"), vm.SCOPE_NOTICE),
        (Forbidden("project"), "Not authorized."),
        (ValidationError("slug", "too short"), "Invalid slug: too short."),
        (Conflict("project"), "That project already exists."),
        (NotFound("api key"), "The api key was not found; it may have been deleted."),
        (StorageUnavailable(), "Storage is unavailable. Try again shortly."),
    ],
)
def test_action_error_messages(exc: Exception, message: str) -> None:
    assert vm.action_error(exc) == message


def test_unknown_failures_log_the_class_only_req_323(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, "agentlens.ui"):
        msg = vm.action_error(RuntimeError(f"boom {PLAIN}"))
    assert msg == "Something went wrong. Check the server log."
    assert "RuntimeError" in caplog.text
    assert PLAIN not in caplog.text


def test_tab_and_href() -> None:
    assert vm.settings_tab({}) == "projects"
    assert vm.settings_tab({"tab": ["pricing"]}) == "pricing"
    assert vm.settings_tab({"tab": "nope"}) == "projects"
    assert vm.settings_href("/obs/", "projects") == "/obs/settings"
    assert vm.settings_href("/obs", "redaction", "01P") == "/obs/settings?tab=redaction&project=01P"


def test_pick_project_by_id_or_slug() -> None:
    projects = [project("01A", "alpha"), project("01B", "beta")]
    assert vm.pick_project(projects, "01B") == projects[1]
    assert vm.pick_project(projects, "beta") == projects[1]
    assert vm.pick_project(projects, "zzz") == projects[0]
    assert vm.pick_project([], "x") is None


@pytest.mark.parametrize(
    ("name", "slug"),
    [("My Shop", "my-shop"), ("  Ünïcode  App!! ", "n-code-app"), ("ab", ""), ("x" * 60, "x" * 40)],
)
def test_suggest_slug(name: str, slug: str) -> None:
    assert vm.suggest_slug(name) == slug


def test_project_rows() -> None:
    (row, forever) = vm.project_rows(
        [project("01A", "alpha"), project("01B", "beta", retention_days=0, content_storage=False)]
    )
    assert (row.retention, row.content) == ("30 days", "Stored")
    assert (forever.retention, forever.content) == ("Forever", "Not stored")
    assert vm.retention_label(1) == "1 day"


def test_key_rows_active_first_req_170c() -> None:
    rows = vm.key_rows([key("a", T0), key("b", T0 + 5, revoked_at=T0 + 9), key("c", T0 + 1)])
    assert [r.key_id for r in rows] == ["c", "a", "b"]
    assert [r.active for r in rows] == [True, True, False]
    assert rows[2].status.startswith("Revoked ")
    assert rows[0].last_used == "Never"
    assert rows[0].label == "—"


def test_reveal_holds_the_plaintext_but_never_prints_it_req_170b() -> None:
    created = CreatedApiKey(key_id="k1", plaintext=SecretStr(PLAIN), snippet=f"x={PLAIN}")
    reveal = vm.Reveal.of(created)
    assert (reveal.plaintext, reveal.snippet) == (PLAIN, f"x={PLAIN}")
    assert PLAIN not in repr(reveal)
    assert PLAIN not in str(reveal)
    assert PLAIN not in f"{reveal}"
    assert vm.EMPTY_REVEAL.key_id == vm.EMPTY_REVEAL.plaintext == ""
