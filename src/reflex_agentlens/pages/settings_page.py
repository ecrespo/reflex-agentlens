"""Settings (plan tasks P6-10.1 to 10.3; REQ-170 to 174, 231.b).

One page with tabs (``?tab=``) for one selected Project (``?project=``, ID or slug). A viewer
without admin rights sees only the "requires admin" state, and every mutating event checks the
viewer again before calling ``AdminService`` (which checks a third time). A failed action shows
an inline message and leaves the page in place (:func:`views.settings.action_error`).

The plaintext of a new API key lives in :attr:`SettingsState.reveal` only while its dialog is
open: closing the dialog, choosing another Project or reloading the page clears it (REQ-170.b).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

import reflex as rx
from reflex.event import EventSpec

from reflex_agentlens.pages._base import AgentlensState, page_frame, ui_event
from reflex_agentlens.query.dto import Viewer
from reflex_agentlens.views import settings as vm
from reflex_agentlens.views.registry import registry, services

__all__ = ["ON_LOAD", "TITLE", "SettingsState", "page"]

TITLE = "Settings"
T = TypeVar("T")


class SettingsState(AgentlensState):
    loaded: bool = False
    tab: str = "projects"
    can_view: bool = False
    can_mutate: bool = False
    access_notice: str = ""
    action_error: str = ""
    action_done: str = ""
    projects: list[vm.ProjectRow] = []
    project_id: str = ""
    project_name: str = ""
    keys: list[vm.KeyRow] = []
    reveal: vm.Reveal = vm.EMPTY_REVEAL
    delete_open: bool = False
    new_name: str = ""
    new_slug: str = ""

    # ── helpers ──────────────────────────────────────────────────────────────────────────────

    def _params(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(str(self.router.url)).query)

    def _access(self, viewer: Viewer) -> vm.Access:
        _, admin = services()
        return vm.access(viewer.is_admin, admin.demo or registry().demo)

    def _apply_access(self, acc: vm.Access) -> None:
        self.can_view, self.can_mutate, self.access_notice = acc.visible, acc.can_mutate, acc.notice

    async def _act(self, viewer: Viewer, call: Callable[[], Awaitable[T]], done: str) -> T | None:
        """Run one settings mutation: checked here first (REQ-174.a), errors shown inline."""
        acc = self._access(viewer)
        self._apply_access(acc)
        self.action_error = self.action_done = ""
        if not acc.can_mutate:
            self.action_error = acc.notice
            return None
        try:
            result = await call()
        except Exception as exc:
            self.action_error = vm.action_error(exc)
            return None
        self.action_done = done
        return result

    async def _load_projects(self, viewer: Viewer, wanted: str) -> None:
        _, admin = services()
        projects = await admin.list_projects_admin(viewer)
        self.projects = vm.project_rows(projects)
        chosen = vm.pick_project(projects, wanted)
        self.project_id = chosen.id if chosen else ""
        self.project_name = chosen.name if chosen else ""

    async def _load_tab(self, viewer: Viewer) -> None:
        if not self.project_id:
            self.keys = []
            return
        _, admin = services()
        if self.tab == "projects":
            self.keys = vm.key_rows(await admin.list_api_keys(viewer, self.project_id))

    # ── events ───────────────────────────────────────────────────────────────────────────────

    @ui_event
    async def load_settings(self, viewer: Viewer) -> None:
        params = self._params()
        self.reveal, self.delete_open = vm.EMPTY_REVEAL, False
        self.action_error = self.action_done = ""
        self.tab = vm.settings_tab(params)
        self._apply_access(self._access(viewer))
        self.loaded = True
        if not self.can_view:  # REQ-174.a: nothing is read for a non-admin viewer
            self.projects, self.keys, self.project_id, self.project_name = [], [], "", ""
            return
        wanted = params.get("project", [""])[0]
        await self._load_projects(viewer, wanted)
        await self._load_tab(viewer)

    @rx.event
    def pick_tab(self, tab: str) -> EventSpec:
        return rx.redirect(vm.settings_href(registry().prefix, tab, self.project_id))

    @rx.event
    def pick_project(self, project_id: str) -> EventSpec:
        return rx.redirect(vm.settings_href(registry().prefix, self.tab, project_id))

    @rx.event
    def set_new_name(self, name: str) -> None:
        self.new_name = name
        self.new_slug = vm.suggest_slug(name)

    @rx.event
    def set_new_slug(self, slug: str) -> None:
        self.new_slug = slug

    @ui_event
    async def create_project(self, viewer: Viewer, data: dict[str, Any]) -> None:
        """REQ-170.a: created with the configured defaults."""
        name, slug = str(data.get("name", "")), str(data.get("slug", ""))
        _, admin = services()
        made = await self._act(
            viewer, lambda: admin.create_project(viewer, name, slug), "Project created."
        )
        if made is not None:
            self.new_name = self.new_slug = ""
            await self._load_projects(viewer, made.id)
            await self._load_tab(viewer)

    @rx.event
    def ask_delete(self) -> None:
        self.delete_open = True

    @rx.event
    def cancel_delete(self) -> None:
        self.delete_open = False

    @ui_event
    async def delete_project(self, viewer: Viewer, data: dict[str, Any]) -> None:
        """REQ-170.d: the typed name must match exactly (checked by the service too)."""
        typed = str(data.get("confirm", ""))
        if typed != self.project_name:
            self.action_error = "Type the Project name exactly to delete it."
            return
        _, admin = services()
        project_id = self.project_id
        await self._act(
            viewer,
            lambda: admin.delete_project(viewer, project_id, typed),
            "Project deleted with its keys and trace data.",
        )
        if self.action_error:  # the result is None either way: the error tells them apart
            return
        self.delete_open = False
        await self._load_projects(viewer, "")
        await self._load_tab(viewer)

    @ui_event
    async def create_key(self, viewer: Viewer, data: dict[str, Any]) -> None:
        """REQ-170.b: the plaintext is shown once, in the dialog."""
        label = str(data.get("label", ""))
        _, admin = services()
        project_id = self.project_id
        created = await self._act(
            viewer, lambda: admin.create_api_key(viewer, project_id, label), "API key created."
        )
        if created is not None:
            self.reveal = vm.Reveal.of(created)
            await self._load_tab(viewer)

    @rx.event
    def close_reveal(self) -> None:
        """The plaintext leaves the state for good (REQ-170.b)."""
        self.reveal = vm.EMPTY_REVEAL

    @ui_event
    async def revoke_key(self, viewer: Viewer, key_id: str) -> None:
        """REQ-170.c: ingest rejects the key within 5 s (REQ-006.c)."""
        _, admin = services()
        await self._act(viewer, lambda: admin.revoke_api_key(viewer, key_id), "API key revoked.")
        await self._load_tab(viewer)


ON_LOAD = [AgentlensState.check_auth, SettingsState.load_settings]

# ── components ───────────────────────────────────────────────────────────────────────────────

S = SettingsState


def _tabs() -> rx.Component:
    return rx.el.nav(
        *[
            rx.el.button(
                label,
                type="button",
                on_click=S.pick_tab(key),  # type: ignore[operator]
                aria_current=rx.cond(S.tab == key, "page", "false"),
                class_name="agentlens-tab",
            )
            for key, label in vm.TABS.items()
        ],
        aria_label="Settings sections",
        class_name="agentlens-tabs",
    )


def _notices() -> rx.Component:
    return rx.fragment(
        rx.cond(S.access_notice != "",
                rx.el.p(S.access_notice, role="status", class_name="agentlens-notice")),
        rx.cond(S.action_error != "",
                rx.el.p(S.action_error, role="alert", class_name="agentlens-error")),
        rx.cond(S.action_done != "",
                rx.el.p(S.action_done, role="status", class_name="agentlens-done")),
    )  # fmt: skip


def _project_picker() -> rx.Component:
    return rx.el.label(
        rx.el.span("Project"),
        rx.el.select(
            rx.foreach(S.projects, lambda p: rx.el.option(p.name, value=p.id)),
            value=S.project_id,
            on_change=S.pick_project,
        ),
    )


def _project_row(p: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(p.name), rx.el.td(rx.el.code(p.slug)), rx.el.td(p.retention),
        rx.el.td(p.content), rx.el.td(p.created),
    )  # fmt: skip


def _create_project() -> rx.Component:
    return rx.el.form(
        rx.el.h3("New Project"),
        rx.el.label(rx.el.span("Name"),
                    rx.el.input(name="name", value=S.new_name, on_change=S.set_new_name,
                                required=True, max_length=100)),
        rx.el.label(rx.el.span("Slug"),
                    rx.el.input(name="slug", value=S.new_slug, on_change=S.set_new_slug,
                                required=True, max_length=40)),
        rx.el.button("Create Project", type="submit"),
        on_submit=S.create_project,
        reset_on_submit=False,
    )  # fmt: skip


def _delete_project() -> rx.Component:
    return rx.cond(
        S.delete_open,
        rx.el.form(
            rx.el.p("Deleting ", rx.el.strong(S.project_name),
                    " removes its API keys and all of its trace data. This cannot be undone."),
            rx.el.label(rx.el.span("Type the Project name to confirm"),
                        rx.el.input(name="confirm", auto_complete="off", required=True)),
            rx.el.button("Delete Project", type="submit", class_name="agentlens-danger"),
            rx.el.button("Cancel", type="button", on_click=S.cancel_delete),
            on_submit=S.delete_project,
            role="alertdialog",
        ),
        rx.el.button("Delete this Project…", type="button", on_click=S.ask_delete,
                     class_name="agentlens-danger"),
    )  # fmt: skip


def _key_row(k: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(rx.el.code(k.prefix)), rx.el.td(k.label), rx.el.td(k.created),
        rx.el.td(k.last_used), rx.el.td(k.status),
        rx.el.td(rx.cond(k.active & S.can_mutate,
                         rx.el.button("Revoke", type="button",
                                      on_click=S.revoke_key(k.key_id)))),
    )  # fmt: skip


def _reveal() -> rx.Component:
    return rx.cond(
        S.reveal.key_id != "",
        rx.el.div(
            rx.el.h3("New API key"),
            rx.el.p("Copy the key now: it is shown only once and cannot be recovered."),
            rx.el.pre(rx.el.code(S.reveal.plaintext)),
            rx.el.button("Copy key", type="button", on_click=rx.set_clipboard(S.reveal.plaintext)),
            rx.el.p("Configure an OpenTelemetry exporter:"),
            rx.el.pre(rx.el.code(S.reveal.snippet)),
            rx.el.button("Copy snippet", type="button",
                         on_click=rx.set_clipboard(S.reveal.snippet)),
            rx.el.button("Done", type="button", on_click=S.close_reveal),
            role="dialog",
            aria_modal="true",
            aria_label="New API key",
            class_name="agentlens-dialog",
        ),
    )  # fmt: skip


def projects_tab() -> rx.Component:
    keys = rx.el.section(
        rx.el.h3("API keys"),
        rx.cond(S.keys.length() > 0,  # type: ignore[attr-defined]
                rx.el.table(
                    rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in
                                           ("Prefix", "Label", "Created (UTC)", "Last used",
                                            "Status", "")])),
                    rx.el.tbody(rx.foreach(S.keys, _key_row)),
                    class_name="agentlens-table"),
                rx.el.p("No API keys for this Project yet.")),
        rx.cond(S.can_mutate,
                rx.el.form(
                    rx.el.label(rx.el.span("Label (optional)"),
                                rx.el.input(name="label", max_length=100)),
                    rx.el.button("Create API key", type="submit"),
                    on_submit=S.create_key, reset_on_submit=True)),
        _reveal(),
    )  # fmt: skip
    return rx.fragment(
        rx.el.table(
            rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in
                                   ("Name", "Slug", "Retention", "Content", "Created (UTC)")])),
            rx.el.tbody(rx.foreach(S.projects, _project_row)),
            class_name="agentlens-table",
        ),
        rx.cond(S.can_mutate, _create_project()),
        rx.cond(S.project_id != "",
                rx.fragment(keys, rx.cond(S.can_mutate, _delete_project()))),
    )  # fmt: skip


def requires_admin() -> rx.Component:
    return rx.el.div(
        rx.el.h2("Requires admin"),
        rx.el.p(vm.REQUIRES_ADMIN),
        class_name="agentlens-denied",
    )


def body() -> rx.Component:
    return rx.cond(
        S.can_view,
        rx.fragment(
            _tabs(),
            _notices(),
            rx.cond(S.projects.length() > 0, _project_picker()),  # type: ignore[attr-defined]
            rx.match(S.tab, ("projects", projects_tab()), projects_tab()),
        ),
        rx.cond(S.loaded, requires_admin()),
    )


def page() -> rx.Component:
    return page_frame(TITLE, body())
