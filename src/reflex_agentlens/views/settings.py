"""Settings view model: access, tabs, Projects and API keys (plan task P6-10.1; REQ-170, 174,
231.b).

- :func:`access`: what the viewer may do. A viewer without admin rights sees only the "requires
  admin" state (REQ-174.a); in demo mode Settings is read-only (REQ-231.b). The services check
  the same rules again on every call.
- :func:`action_error`: the inline message of a failed settings action. Unlike page-level
  errors it never hides the page: a refused mutation leaves the settings visible.
- :class:`Reveal`: the plaintext of a new API key and its ``OTEL_EXPORTER_OTLP_*`` snippet,
  shown once (REQ-170.b). The page clears it when the dialog closes and on every load.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from reflex_agentlens.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    StorageUnavailable,
    ValidationError,
)
from reflex_agentlens.query.dto import ApiKeyView, CreatedApiKey, Project
from reflex_agentlens.views.fmt import DASH, fmt_time

__all__ = [
    "DEMO_NOTICE",
    "EMPTY_REVEAL",
    "REQUIRES_ADMIN",
    "TABS",
    "Access",
    "KeyRow",
    "ProjectRow",
    "Reveal",
    "access",
    "action_error",
    "key_rows",
    "pick_project",
    "project_rows",
    "retention_label",
    "settings_href",
    "settings_tab",
    "suggest_slug",
]

log = logging.getLogger("agentlens.ui")

TABS: dict[str, str] = {
    "projects": "Projects and keys",
    "pricing": "Pricing",
    "redaction": "Redaction",
    "retention": "Retention and storage",
}
DEFAULT_TAB = "projects"
REQUIRES_ADMIN = "Settings require admin rights."
DEMO_NOTICE = "Read-only: this is a demo instance, so settings cannot be changed."
SCOPE_NOTICE = "This change needs access to every Project."
_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
MAX_SLUG = 40

Params = Mapping[str, str | Sequence[str]]


@dataclass(frozen=True, slots=True)
class Access:
    visible: bool
    can_mutate: bool
    notice: str


def access(is_admin: bool, demo: bool) -> Access:
    if not is_admin:
        return Access(visible=False, can_mutate=False, notice=REQUIRES_ADMIN)
    if demo:
        return Access(visible=True, can_mutate=False, notice=DEMO_NOTICE)
    return Access(visible=True, can_mutate=True, notice="")


def action_error(exc: BaseException) -> str:
    """The message of a failed settings action (Tech Design §5.5, REQ-323: unknown failures
    log the exception class only)."""
    if isinstance(exc, Forbidden):
        if exc.reason == "requires admin":
            return REQUIRES_ADMIN
        if exc.reason == "demo mode":
            return DEMO_NOTICE
        return SCOPE_NOTICE if "every Project" in exc.reason else "Not authorized."
    if isinstance(exc, ValidationError):
        return f"Invalid {exc.field}: {exc.reason}."
    if isinstance(exc, Conflict):
        return f"That {exc.entity} already exists."
    if isinstance(exc, NotFound):
        return f"The {exc.entity} was not found; it may have been deleted."
    if isinstance(exc, StorageUnavailable):
        return "Storage is unavailable. Try again shortly."
    log.error("settings action failed (%s)", type(exc).__name__)
    return "Something went wrong. Check the server log."


# ── URL (tab and Project) ────────────────────────────────────────────────────────────────────


def _first(params: Params, key: str) -> str:
    value = params.get(key)
    if value is None or isinstance(value, str):
        return value or ""
    return value[0] if value else ""


def settings_tab(params: Params) -> str:
    tab = _first(params, "tab")
    return tab if tab in TABS else DEFAULT_TAB


def settings_href(prefix: str, tab: str, project: str = "") -> str:
    pairs = [] if tab == DEFAULT_TAB else [("tab", tab)]
    if project:
        pairs.append(("project", project))
    base = f"{prefix.rstrip('/')}/settings"
    return f"{base}?{urlencode(pairs)}" if pairs else base


def pick_project(projects: Sequence[Project], wanted: str) -> Project | None:
    """The Project named by ``?project=`` (ID or slug), else the first one."""
    for p in projects:
        if wanted and wanted in (p.id, p.slug):
            return p
    return projects[0] if projects else None


def suggest_slug(name: str) -> str:
    """A slug for a new Project name; the store validates the final value."""
    slug = _SLUG_CHARS.sub("-", name.lower()).strip("-")[:MAX_SLUG].strip("-")
    return slug if len(slug) >= 3 else ""


# ── rows ─────────────────────────────────────────────────────────────────────────────────────


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True)


def retention_label(days: int) -> str:
    if days <= 0:
        return "Forever"
    return "1 day" if days == 1 else f"{days} days"


class ProjectRow(_Row):
    id: str
    slug: str
    name: str
    retention: str
    content: str
    created: str


def project_rows(projects: Iterable[Project]) -> list[ProjectRow]:
    return [
        ProjectRow(
            id=p.id,
            slug=p.slug,
            name=p.name,
            retention=retention_label(p.retention_days),
            content="Stored" if p.content_storage else "Not stored",
            created=fmt_time(p.created_at_ns),
        )
        for p in projects
    ]


class KeyRow(_Row):
    key_id: str
    prefix: str
    label: str
    created: str
    last_used: str
    status: str
    active: bool


def key_rows(keys: Iterable[ApiKeyView]) -> list[KeyRow]:
    """Active keys first, newest first within each group."""
    ordered = sorted(keys, key=lambda k: (k.revoked, -k.created_at_ns, k.key_id))
    return [
        KeyRow(
            key_id=k.key_id,
            prefix=k.prefix,
            label=k.label or DASH,
            created=fmt_time(k.created_at_ns),
            last_used=fmt_time(k.last_used_at_ns) if k.last_used_at_ns else "Never",
            status=f"Revoked {fmt_time(k.revoked_at_ns)}" if k.revoked else "Active",
            active=not k.revoked,
        )
        for k in ordered
    ]


class Reveal(_Row):
    """A new key's plaintext and snippet: shown once, then cleared (REQ-170.b)."""

    key_id: str = ""
    plaintext: str = ""
    snippet: str = ""

    @classmethod
    def of(cls, created: CreatedApiKey) -> Reveal:
        return cls(
            key_id=created.key_id,
            plaintext=created.plaintext.get_secret_value(),
            snippet=created.snippet,
        )

    def __repr__(self) -> str:  # the plaintext never reaches a log line through repr
        return f"Reveal(key_id={self.key_id!r})"

    __str__ = __repr__


EMPTY_REVEAL = Reveal()
