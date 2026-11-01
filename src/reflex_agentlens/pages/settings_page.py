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
from reflex_agentlens.query.dto import ProjectSettingsPatch, Viewer
from reflex_agentlens.views import pricing as pv
from reflex_agentlens.views import redaction as rv
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
    # pricing (P6-10.2)
    price_rows: list[pv.PriceRow] = []
    price_errors: list[str] = []
    price_current: int = 0
    price_viewing: int = 0
    price_label: str = ""
    price_versions: list[dict[str, str]] = []
    unpriced: list[pv.UnpricedRow] = []
    # redaction (P6-10.3)
    presets: dict[str, bool] = {}
    pattern_rows: list[rv.PatternRow] = []
    field_rows: list[rv.FieldRow] = []
    rule_errors: list[str] = []
    preview_out: str = ""
    preview_error: str = ""
    # retention and storage (P6-10.3)
    retention_text: str = ""
    content_storage: bool = True
    storage_usage: str = ""
    storage_limit: str = ""
    storage_counts: str = ""
    last_purge: str = ""
    storage_note: str = ""

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
        self.retention_text = str(chosen.retention_days) if chosen else ""
        self.content_storage = chosen.content_storage if chosen else True

    async def _load_tab(self, viewer: Viewer) -> None:
        _, admin = services()
        if self.tab == "pricing":  # global, not per Project
            await self._load_pricing(viewer, None)
        elif self.tab == "redaction" and self.project_id:
            await self._load_redaction(viewer)
        elif self.tab == "retention":
            await self._load_storage(viewer)
        elif self.tab == "projects":
            self.keys = (
                vm.key_rows(await admin.list_api_keys(viewer, self.project_id))
                if self.project_id
                else []
            )

    async def _load_pricing(self, viewer: Viewer, version: int | None) -> None:
        _, admin = services()
        versions = await admin.list_pricing_versions(viewer)
        self.unpriced = pv.unpriced_rows(await admin.list_unpriced_models(viewer))
        self.price_errors = []
        if not versions:  # nothing saved yet: the first save creates version 1
            self.price_current = self.price_viewing = 0
            self.price_label, self.price_versions, self.price_rows = "No prices yet.", [], []
            return
        table = await admin.get_pricing(viewer, version)
        self.price_current, self.price_viewing = versions[0].version, table.info.version
        self.price_label = pv.version_label(table.info)
        self.price_versions = [{"value": str(v.version), "label": pv.version_label(v)}
                               for v in versions]  # fmt: skip
        self.price_rows = pv.price_rows(table)

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

    async def _load_redaction(self, viewer: Viewer) -> None:
        _, admin = services()
        rules = await admin.get_redaction_rules(viewer, self.project_id)
        self.presets, self.pattern_rows, self.field_rows = rv.redaction_form(rules)
        self.rule_errors, self.preview_out, self.preview_error = [], "", ""

    async def _load_storage(self, viewer: Viewer) -> None:
        """REQ-173: instance-wide numbers only for an admin who sees every Project."""
        query, _ = services()
        if viewer.allowed_project_ids is not None:
            self.storage_note = "Instance-wide storage is shown to admins of every Project."
            self.storage_usage = self.storage_limit = self.storage_counts = self.last_purge = ""
            return
        view = rv.storage_view(await query.storage_status(viewer))
        self.storage_note = ""
        self.storage_usage, self.storage_limit = view.usage, view.limit
        self.storage_counts, self.last_purge = view.counts, view.last_purge

    def _edited_rules(self) -> Any:
        rules, errors = rv.rules_from_form(self.presets, self.pattern_rows, self.field_rows)
        self.rule_errors = errors
        return rules

    # ── redaction (REQ-039.f, 172) ───────────────────────────────────────────────────────────

    @rx.event
    def toggle_preset(self, name: str) -> None:
        if name in rv.PRESETS:
            self.presets = {**self.presets, name: not self.presets.get(name, False)}

    @rx.event
    def set_pattern(self, index: int, field: str, value: str) -> None:
        if field in ("name", "regex") and 0 <= index < len(self.pattern_rows):
            rows = list(self.pattern_rows)
            rows[index] = rows[index].model_copy(update={field: value[:2048]})
            self.pattern_rows = rows

    @rx.event
    def toggle_pattern(self, index: int) -> None:
        if 0 <= index < len(self.pattern_rows):
            rows = list(self.pattern_rows)
            rows[index] = rows[index].model_copy(update={"enabled": not rows[index].enabled})
            self.pattern_rows = rows

    @rx.event
    def add_pattern_row(self) -> None:
        self.pattern_rows = [*self.pattern_rows, rv.PatternRow()]

    @rx.event
    def remove_pattern_row(self, index: int) -> None:
        self.pattern_rows = [r for i, r in enumerate(self.pattern_rows) if i != index]

    @rx.event
    def set_field_rule(self, index: int, field: str, value: str) -> None:
        if field in ("key_glob", "action") and 0 <= index < len(self.field_rows):
            rows = list(self.field_rows)
            rows[index] = rows[index].model_copy(update={field: value[:256]})
            self.field_rows = rows

    @rx.event
    def add_field_row(self) -> None:
        self.field_rows = [*self.field_rows, rv.FieldRow()]

    @rx.event
    def remove_field_row(self, index: int) -> None:
        self.field_rows = [r for i, r in enumerate(self.field_rows) if i != index]

    @ui_event
    async def save_redaction(self, viewer: Viewer) -> None:
        """REQ-172.a: regexes are compiled and timed by the service at save."""
        rules = self._edited_rules()
        if rules is None:
            self.action_error = "Fix the redaction rules before saving."
            return
        _, admin = services()
        project_id = self.project_id
        await self._act(
            viewer,
            lambda: admin.save_redaction_rules(viewer, project_id, rules),
            f"Redaction rules saved. {rv.NOT_REWRITTEN}",
        )
        if self.action_error:
            self.action_error = rv.rule_error_label(self.action_error)
            return
        await self._load_redaction(viewer)
        self.action_done = f"Redaction rules saved. {rv.NOT_REWRITTEN}"

    @ui_event
    async def run_preview(self, viewer: Viewer, data: dict[str, Any]) -> None:
        """REQ-172.b (SHOULD): the unsaved rules on a sample; nothing is stored or logged.
        Allowed in demo mode because it changes nothing."""
        self.preview_out = self.preview_error = ""
        rules = self._edited_rules()
        if rules is None:
            self.preview_error = "Fix the redaction rules to preview them."
            return
        sample = str(data.get("sample", ""))
        if not sample.strip():
            self.preview_error = "Paste sample text or a span as JSON."
            return
        _, admin = services()
        try:
            self.preview_out = admin.preview_redaction(viewer, rules, sample)
        except Exception as exc:
            self.preview_error = rv.rule_error_label(vm.action_error(exc))

    # ── retention (REQ-173) ──────────────────────────────────────────────────────────────────

    @rx.event
    def toggle_content(self) -> None:
        self.content_storage = not self.content_storage

    @ui_event
    async def save_retention(self, viewer: Viewer, data: dict[str, Any]) -> None:
        days, problem = rv.retention_days(str(data.get("days", "")))
        if days is None:
            self.action_error = problem
            return
        patch = ProjectSettingsPatch(retention_days=days, content_storage=self.content_storage)
        _, admin = services()
        project_id = self.project_id
        saved = await self._act(
            viewer,
            lambda: admin.update_project_settings(viewer, project_id, patch),
            "Retention settings saved.",
        )
        if saved is not None:
            await self._load_projects(viewer, saved.id)

    # ── pricing (REQ-060.c, 171) ─────────────────────────────────────────────────────────────

    @ui_event
    async def pick_price_version(self, viewer: Viewer, version: str) -> None:
        """Older versions are shown read-only; edits always start from the current one."""
        await self._load_pricing(viewer, int(version) if version.isdigit() else None)

    @rx.event
    def set_price_cell(self, index: int, field: str, value: str) -> None:
        if field not in pv.COLUMNS or not 0 <= index < len(self.price_rows):
            return
        rows = list(self.price_rows)
        rows[index] = rows[index].model_copy(update={field: value[:200]})
        self.price_rows = rows

    @rx.event
    def add_price_row(self) -> None:
        self.price_rows = [*self.price_rows, pv.PriceRow()]

    @rx.event
    def remove_price_row(self, index: int) -> None:
        self.price_rows = [r for i, r in enumerate(self.price_rows) if i != index]

    @rx.event
    def add_unpriced(self, provider: str, model: str) -> None:
        """REQ-171.b: one click adds an exact entry to fill in, then save."""
        self.price_rows = [*self.price_rows, pv.PriceRow(provider=provider, pattern=model)]
        self.unpriced = [u for u in self.unpriced if (u.provider, u.model) != (provider, model)]

    @ui_event
    async def save_pricing(self, viewer: Viewer, data: dict[str, Any]) -> None:
        """REQ-060.c: saved as a new version; earlier versions are unchanged."""
        if self.price_viewing != self.price_current:
            self.action_error = "Switch to the current version to edit prices."
            return
        entries, errors = pv.entries_from_rows(self.price_rows)
        self.price_errors = errors
        if errors:
            self.action_error = "Fix the highlighted prices before saving."
            return
        note = str(data.get("note", "")).strip() or None
        _, admin = services()
        saved = await self._act(
            viewer, lambda: admin.save_pricing(viewer, entries, note), "Prices saved."
        )
        if saved is not None:
            self.action_done = (
                f"Saved as version {saved.info.version}. Calls already stored keep their cost "
                "until you recompute them."
            )
            await self._load_pricing(viewer, None)

    @ui_event
    async def export_pricing(self, viewer: Viewer) -> EventSpec:
        """REQ-171.c: the current version as CSV (reading, so allowed in demo mode)."""
        _, admin = services()
        text = await admin.export_pricing_csv(viewer)
        return rx.download(data=text, filename=pv.csv_filename(self.price_current))

    @ui_event
    async def import_pricing(self, viewer: Viewer, data: dict[str, Any]) -> None:
        """REQ-171.c: a pasted CSV becomes a new version; errors name the line."""
        text = str(data.get("csv", ""))
        if not text.strip():
            self.action_error = "Paste a pricing CSV first."
            return
        _, admin = services()
        saved = await self._act(
            viewer, lambda: admin.import_pricing_csv(viewer, text), "Prices imported."
        )
        if saved is not None:
            self.action_done = f"Imported as version {saved.info.version}."
            await self._load_pricing(viewer, None)

    @ui_event
    async def recompute(self, viewer: Viewer, data: dict[str, Any]) -> None:
        """REQ-062: re-price the selected Project's LLM calls with the current prices."""
        since = str(data.get("from", "")).strip() or "now-7d"
        _, admin = services()
        project_id = self.project_id
        count = await self._act(
            viewer, lambda: admin.recompute_costs(viewer, project_id, since), "Costs recomputed."
        )
        if count is not None:
            self.action_done = f"{count:,} LLM calls re-priced in {self.project_name}."
            if self.tab == "pricing":
                self.unpriced = pv.unpriced_rows(await admin.list_unpriced_models(viewer))

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


def _price_cell(row: Any, index: Any, field: str) -> rx.Component:
    return rx.el.td(
        rx.el.input(
            value=row[field],
            on_change=lambda v: S.set_price_cell(index, field, v),  # type: ignore[operator]
            read_only=~S.can_mutate | (S.price_viewing != S.price_current),
            aria_label=pv.COLUMNS[field],
            input_mode="decimal" if field not in ("provider", "pattern") else "text",
            size=10 if field not in ("provider", "pattern") else 24,
        )
    )


def _price_row(row: Any, index: Any) -> rx.Component:
    editable = S.can_mutate & (S.price_viewing == S.price_current)
    return rx.el.tr(
        *[_price_cell(row, index, f) for f in pv.COLUMNS],
        rx.el.td(rx.cond(editable, rx.el.button(
            "Remove", type="button",
            on_click=S.remove_price_row(index)))),  # type: ignore[operator]
    )  # fmt: skip


def _unpriced_row(u: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(u.provider), rx.el.td(rx.el.code(u.model)), rx.el.td(u.calls),
        rx.el.td(u.last_seen),
        rx.el.td(rx.cond(S.can_mutate & (S.price_viewing == S.price_current),
                         rx.el.button("Add price", type="button",
                                      on_click=S.add_unpriced(u.provider, u.model)))),  # type: ignore[operator]
    )  # fmt: skip


def pricing_tab() -> rx.Component:
    editable = S.can_mutate & (S.price_viewing == S.price_current)
    return rx.fragment(
        rx.el.label(
            rx.el.span("Version"),
            rx.el.select(
                rx.foreach(S.price_versions, lambda v: rx.el.option(v["label"], value=v["value"])),
                value=S.price_viewing.to_string(),  # type: ignore[attr-defined]
                on_change=S.pick_price_version,
            ),
        ),
        rx.el.p(S.price_label),
        rx.cond(S.price_viewing != S.price_current,
                rx.el.p("An earlier version, read-only. Prices of past calls keep the version "
                        "they were computed with.", role="status")),
        rx.cond(S.price_errors.length() > 0,  # type: ignore[attr-defined]
                rx.el.ul(rx.foreach(S.price_errors, rx.el.li), role="alert",
                         class_name="agentlens-error")),
        rx.el.form(
            rx.el.table(
                rx.el.caption("Prices in USD per 1M tokens; * in a pattern matches any text."),
                rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in pv.COLUMNS.values()],
                                     rx.el.th("", scope="col"))),
                rx.el.tbody(rx.foreach(S.price_rows, _price_row)),
                class_name="agentlens-table",
            ),
            rx.cond(editable, rx.fragment(
                rx.el.button("Add entry", type="button", on_click=S.add_price_row),
                rx.el.label(rx.el.span("Note (optional)"),
                            rx.el.input(name="note", max_length=500)),
                rx.el.button("Save as new version", type="submit"),
            )),
            on_submit=S.save_pricing,
            reset_on_submit=True,
        ),
        rx.el.section(
            rx.el.h3("Unpriced models"),
            rx.cond(S.unpriced.length() > 0,  # type: ignore[attr-defined]
                    rx.el.table(
                        rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in
                                               ("Provider", "Model", "Calls", "Last seen", "")])),
                        rx.el.tbody(rx.foreach(S.unpriced, _unpriced_row)),
                        class_name="agentlens-table"),
                    rx.el.p("Every model seen so far has a price.")),
        ),
        rx.el.section(
            rx.el.h3("CSV"),
            rx.el.button("Export current version", type="button", on_click=S.export_pricing),
            rx.cond(S.can_mutate, rx.el.form(
                rx.el.label(rx.el.span("Paste a pricing CSV to import it as a new version"),
                            rx.el.textarea(name="csv", rows=6)),
                rx.el.button("Import CSV", type="submit"),
                on_submit=S.import_pricing, reset_on_submit=True,
            )),
        ),
        rx.cond(S.can_mutate & (S.project_id != ""), rx.el.section(
            rx.el.h3("Recompute costs"),
            rx.el.p("Re-price the LLM calls of ", rx.el.strong(S.project_name),
                    " with the current prices."),
            rx.el.form(
                rx.el.label(rx.el.span("Since"),
                            rx.el.input(name="from", default_value="now-7d", max_length=40)),
                rx.el.button("Recompute", type="submit"),
                on_submit=S.recompute, reset_on_submit=False,
            ),
        )),
    )  # fmt: skip


def _preset(name: str, label: str) -> rx.Component:
    return rx.el.label(
        rx.el.input(type="checkbox", checked=S.presets[name], read_only=True,
                    disabled=~S.can_mutate, on_click=S.toggle_preset(name)),  # type: ignore[operator]
        rx.el.span(label),
    )  # fmt: skip


def _pattern_row(row: Any, index: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(rx.el.input(value=row.name, aria_label="Rule name", size=16,
                             read_only=~S.can_mutate,
                             on_change=lambda v: S.set_pattern(index, "name", v))),  # type: ignore[operator]
        rx.el.td(rx.el.input(value=row.regex, aria_label="Regular expression", size=40,
                             read_only=~S.can_mutate, spell_check=False,
                             on_change=lambda v: S.set_pattern(index, "regex", v))),  # type: ignore[operator]
        rx.el.td(rx.el.input(type="checkbox", checked=row.enabled, read_only=True,
                             aria_label="Enabled", disabled=~S.can_mutate,
                             on_click=S.toggle_pattern(index))),  # type: ignore[operator]
        rx.el.td(rx.cond(S.can_mutate, rx.el.button(
            "Remove", type="button", on_click=S.remove_pattern_row(index)))),  # type: ignore[operator]
    )  # fmt: skip


def _field_row(row: Any, index: Any) -> rx.Component:
    return rx.el.tr(
        rx.el.td(rx.el.input(value=row.key_glob, aria_label="Attribute key", size=32,
                             read_only=~S.can_mutate,
                             on_change=lambda v: S.set_field_rule(index, "key_glob", v))),  # type: ignore[operator]
        rx.el.td(rx.el.select(rx.el.option("Mask", value="mask"),
                              rx.el.option("Drop", value="drop"),
                              value=row.action, disabled=~S.can_mutate, aria_label="Action",
                              on_change=lambda v: S.set_field_rule(index, "action", v))),  # type: ignore[operator]
        rx.el.td(rx.cond(S.can_mutate, rx.el.button(
            "Remove", type="button", on_click=S.remove_field_row(index)))),  # type: ignore[operator]
    )  # fmt: skip


def redaction_tab() -> rx.Component:
    return rx.cond(S.project_id != "", rx.fragment(
        rx.el.p(rv.NOT_REWRITTEN, role="note", class_name="agentlens-notice"),
        rx.cond(S.rule_errors.length() > 0,  # type: ignore[attr-defined]
                rx.el.ul(rx.foreach(S.rule_errors, rx.el.li), role="alert",
                         class_name="agentlens-error")),
        rx.el.fieldset(rx.el.legend("Presets"),
                       *[_preset(name, label) for name, label in rv.PRESETS.items()]),
        rx.el.section(
            rx.el.h3("Pattern rules"),
            rx.el.p("Matches in text and attribute values are replaced with [REDACTED:name]."),
            rx.el.table(
                rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in
                                       ("Name", "Regular expression", "Enabled", "")])),
                rx.el.tbody(rx.foreach(S.pattern_rows, _pattern_row)),
                class_name="agentlens-table"),
            rx.cond(S.can_mutate, rx.el.button("Add pattern rule", type="button",
                                               on_click=S.add_pattern_row)),
        ),
        rx.el.section(
            rx.el.h3("Field rules"),
            rx.el.p("Attribute keys (* matches any text) whose values are masked or dropped."),
            rx.el.table(
                rx.el.thead(rx.el.tr(*[rx.el.th(h, scope="col") for h in
                                       ("Attribute key", "Action", "")])),
                rx.el.tbody(rx.foreach(S.field_rows, _field_row)),
                class_name="agentlens-table"),
            rx.cond(S.can_mutate, rx.el.button("Add field rule", type="button",
                                               on_click=S.add_field_row)),
        ),
        rx.cond(S.can_mutate, rx.el.button("Save redaction rules", type="button",
                                           on_click=S.save_redaction)),
        rx.el.section(
            rx.el.h3("Preview"),
            rx.el.form(
                rx.el.label(rx.el.span("Sample text, or a span as JSON (not stored)"),
                            rx.el.textarea(name="sample", rows=6, spell_check=False)),
                rx.el.button("Preview with these rules", type="submit"),
                on_submit=S.run_preview, reset_on_submit=False,
            ),
            rx.cond(S.preview_error != "",
                    rx.el.p(S.preview_error, role="alert", class_name="agentlens-error")),
            rx.cond(S.preview_out != "", rx.el.pre(rx.el.code(S.preview_out))),
        ),
    ))  # fmt: skip


def retention_tab() -> rx.Component:
    return rx.fragment(
        rx.cond(S.project_id != "", rx.el.form(
            rx.el.h3("Retention for ", S.project_name),
            rx.el.label(rx.el.span("Maximum age in days (0 keeps data forever)"),
                        rx.el.input(name="days", default_value=S.retention_text,
                                    key=S.project_id + S.retention_text, input_mode="numeric",
                                    read_only=~S.can_mutate, max_length=4)),
            rx.el.label(rx.el.input(type="checkbox", checked=S.content_storage, read_only=True,
                                    disabled=~S.can_mutate, on_click=S.toggle_content),
                        rx.el.span("Store message and tool content")),
            rx.cond(S.can_mutate, rx.el.button("Save", type="submit")),
            on_submit=S.save_retention, reset_on_submit=False,
        )),
        rx.el.section(
            rx.el.h3("Storage"),
            rx.cond(S.storage_note != "", rx.el.p(S.storage_note),
                    rx.el.dl(
                        rx.el.dt("Usage"), rx.el.dd(S.storage_usage),
                        rx.el.dt("Size limit"), rx.el.dd(S.storage_limit),
                        rx.el.dt("Stored"), rx.el.dd(S.storage_counts),
                        rx.el.dt("Last purge"), rx.el.dd(S.last_purge),
                        class_name="agentlens-pairs")),
        ),
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
            rx.match(
                S.tab,
                ("pricing", pricing_tab()),
                ("redaction", redaction_tab()),
                ("retention", retention_tab()),
                projects_tab(),
            ),
        ),
        rx.cond(S.loaded, requires_admin()),
    )


def page() -> rx.Component:
    return page_frame(TITLE, body())
