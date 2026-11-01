"""``AdminService``: settings mutations (plan tasks P4-05.1, P4-05.2, P4-06.1; API §6.3, §6.5).

Every method checks the viewer first: admin rights (REQ-174), then the Project scope
(REQ-191.c), then demo mode for mutations (REQ-231.b). Saved filters are the exception: any viewer
who can see the Project may list, save and delete them (REQ-102.a).

The store publishes ``settings_changed`` after each settings commit, so ingest drops its caches
(revocation within 5 s, REQ-006.c) without anything else here.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import Forbidden, NotFound, ValidationError
from reflex_agentlens.core.ids import is_ulid, new_ulid
from reflex_agentlens.core.money import price_per_mtok_to_nusd
from reflex_agentlens.core.security import generate_api_key, hash_secret
from reflex_agentlens.ingest.redaction.preview import preview_redaction, validate_rule_set
from reflex_agentlens.query import convert, dto
from reflex_agentlens.query.filters import parse_runs_query, runs_query_string
from reflex_agentlens.query.scope import check_project, resolve_projects, time_range
from reflex_agentlens.store import records as r
from reflex_agentlens.store.pricing_csv import format_pricing_csv, parse_pricing_csv
from reflex_agentlens.store.protocol import StorageBackend

__all__ = ["DEMO_MODE", "AdminService"]

DEMO_MODE = "demo mode"
DAY_S = 86_400
_MAX_LABEL = 100
_MAX_FILTER_NAME = 100
_MAX_NOTE = 500
_MAX_ENTRIES = 5000


class AdminService:
    """Mutations of API §6.3 (``viewer.is_admin`` required except saved filters)."""

    def __init__(
        self,
        store: StorageBackend,
        cfg: AgentlensConfig,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.clock_ns = clock_ns
        self.demo = cfg.demo.enabled

    # ── guards ───────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _admin(viewer: dto.Viewer, project_id: str | None = None) -> None:
        if not viewer.is_admin:
            raise Forbidden("requires admin")
        if project_id is not None:
            check_project(viewer, project_id)

    def _mutation(self, viewer: dto.Viewer, project_id: str | None = None) -> None:
        self._admin(viewer, project_id)
        if self.demo:
            raise Forbidden(DEMO_MODE)

    @staticmethod
    def _full_scope(viewer: dto.Viewer) -> None:
        if viewer.allowed_project_ids is not None:
            raise Forbidden("requires access to every Project")

    async def _project(self, project_id: str) -> r.Project:
        for p in await self.store.list_projects():
            if p.id == project_id:
                return p
        raise NotFound("project")

    # ── Projects (REQ-170, REQ-173) ──────────────────────────────────────────────────────────

    async def list_projects_admin(self, viewer: dto.Viewer) -> list[dto.Project]:
        self._admin(viewer)
        allowed = viewer.allowed_project_ids
        return [
            convert.project(p)
            for p in await self.store.list_projects()
            if allowed is None or p.id in allowed
        ]

    async def create_project(self, viewer: dto.Viewer, name: str, slug: str) -> dto.Project:
        """REQ-170.a: defaults from the configuration (retention, content on, rate limits)."""
        self._mutation(viewer)
        self._full_scope(viewer)  # a limited admin could not see the new Project
        retention = int(self.cfg.retention.default_max_age.total_seconds())
        created = await self.store.create_project(
            r.NewProject(
                id=new_ulid(),
                slug=slug,
                name=name.strip(),
                content_storage=True,
                retention_max_age_s=retention,
                created_by=viewer.id,
            )
        )
        ingest = self.cfg.ingest
        wanted = r.ProjectPatch(
            request_rate=ingest.default_request_rate,
            request_burst=ingest.default_request_burst,
            span_rate=ingest.default_span_rate,
            span_burst=ingest.default_span_burst,
            updated_by=viewer.id,
        )
        current = (
            created.request_rate,
            created.request_burst,
            created.span_rate,
            created.span_burst,
        )
        target = (wanted.request_rate, wanted.request_burst, wanted.span_rate, wanted.span_burst)
        if current != target:
            created = await self.store.update_project(created.id, wanted)
        return convert.project(created)

    async def update_project_settings(
        self, viewer: dto.Viewer, project_id: str, patch: dto.ProjectSettingsPatch
    ) -> dto.Project:
        """REQ-173.a, REQ-040: name, retention, content toggle and rate limits."""
        self._mutation(viewer, project_id)
        days = patch.retention_days
        updated = await self.store.update_project(
            project_id,
            r.ProjectPatch(
                name=None if patch.name is None else patch.name.strip(),
                content_storage=patch.content_storage,
                retention_max_age_s=None if days is None else days * DAY_S,
                request_rate=patch.request_rate,
                request_burst=patch.request_burst,
                span_rate=patch.span_rate,
                span_burst=patch.span_burst,
                updated_by=viewer.id,
            ),
        )
        return convert.project(updated)

    async def delete_project(self, viewer: dto.Viewer, project_id: str, confirm_name: str) -> None:
        """REQ-170.d: the typed name must match; cascades to keys and every trace."""
        self._mutation(viewer, project_id)
        project = await self._project(project_id)
        if confirm_name != project.name:
            raise ValidationError("confirm_name", "does not match the Project name")
        await self.store.delete_project(project_id)

    # ── API keys (REQ-170.b/c) ───────────────────────────────────────────────────────────────

    def _snippet(self, plaintext: str) -> str:
        server = self.cfg.server
        base = str(server.public_url).rstrip("/") if server.public_url else (
            f"http://{server.http_host}:{server.http_port}"
        )  # fmt: skip
        return (
            f'export OTEL_EXPORTER_OTLP_ENDPOINT="{base}"\n'
            'export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"\n'
            f'export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20{plaintext}"\n'
        )

    async def create_api_key(
        self, viewer: dto.Viewer, project_id: str, label: str = ""
    ) -> dto.CreatedApiKey:
        """REQ-170.b: only the salted hash is stored; the plaintext is returned once."""
        self._mutation(viewer, project_id)
        label = label.strip()
        if len(label) > _MAX_LABEL:
            raise ValidationError("label", f"at most {_MAX_LABEL} characters")
        await self._project(project_id)
        key = generate_api_key()
        hashed = hash_secret(key.secret)
        await self.store.create_api_key(
            r.NewApiKeyRecord(
                key_id=key.key_id,
                project_id=project_id,
                salt=hashed.salt,
                secret_hash=hashed.digest,
                created_at_ns=self.clock_ns(),
                label=label,
                created_by=viewer.id,
            )
        )
        return dto.CreatedApiKey(
            key_id=key.key_id,
            plaintext=key.plaintext,  # type: ignore[arg-type]
            snippet=self._snippet(key.plaintext),
        )

    async def list_api_keys(self, viewer: dto.Viewer, project_id: str) -> list[dto.ApiKeyView]:
        self._admin(viewer, project_id)
        return [convert.api_key_view(k) for k in await self.store.list_api_keys(project_id)]

    async def revoke_api_key(self, viewer: dto.Viewer, key_id: str) -> None:
        """REQ-170.c; ingest rejects the key within 5 s (REQ-006.c)."""
        self._admin(viewer)
        record = await self.store.resolve_api_key(key_id)
        if record is None:
            raise NotFound("api key")
        self._mutation(viewer, record.project_id)
        await self.store.revoke_api_key(key_id, self.clock_ns())

    # ── pricing (REQ-060.c, REQ-171) ─────────────────────────────────────────────────────────

    async def get_pricing(self, viewer: dto.Viewer, version: int | None = None) -> dto.PricingTable:
        self._admin(viewer)
        if version is None:
            return convert.pricing_table(await self.store.get_pricing())
        return convert.pricing_table(await self.store.get_pricing_version(version))

    async def list_pricing_versions(self, viewer: dto.Viewer) -> list[dto.PricingVersionInfo]:
        self._admin(viewer)
        metas = await self.store.list_pricing_versions()
        return [convert.pricing_version_info(m) for m in sorted(metas, key=lambda m: -m.version)]

    @staticmethod
    def _entries(entries: Sequence[dto.PricingEntryView]) -> list[r.PricingEntry]:
        """REQ-171.a: non-negative decimals, ``*`` only in glob patterns, no duplicates."""
        if len(entries) > _MAX_ENTRIES:
            raise ValidationError("entries", f"at most {_MAX_ENTRIES} entries")
        out: list[r.PricingEntry] = []
        seen: set[tuple[str, str]] = set()
        for i, e in enumerate(entries):
            where = f"entries[{i}]"
            pattern = e.model_pattern.strip()
            if not pattern:
                raise ValidationError(where, "model_pattern is required")
            if (e.match_kind == "glob") != ("*" in pattern):
                raise ValidationError(where, "glob patterns, and only they, contain *")
            if (e.provider, pattern) in seen:
                raise ValidationError(where, "duplicate provider and model_pattern")
            seen.add((e.provider, pattern))

            def nusd(value: Decimal | None) -> int | None:
                return None if value is None else price_per_mtok_to_nusd(value)

            out.append(
                r.PricingEntry(
                    model_pattern=pattern,
                    match_kind=e.match_kind,
                    input_nusd_per_mtok=price_per_mtok_to_nusd(e.input_usd_per_mtok),
                    output_nusd_per_mtok=price_per_mtok_to_nusd(e.output_usd_per_mtok),
                    provider=e.provider.strip(),
                    cached_input_nusd_per_mtok=nusd(e.cached_input_usd_per_mtok),
                    cache_write_nusd_per_mtok=nusd(e.cache_write_usd_per_mtok),
                )
            )
        return out

    async def save_pricing(
        self,
        viewer: dto.Viewer,
        entries: Sequence[dto.PricingEntryView],
        note: str | None = None,
    ) -> dto.PricingTable:
        """REQ-060.c: a new immutable version; earlier versions never change."""
        self._mutation(viewer)
        self._full_scope(viewer)  # pricing is global: a Project-limited admin cannot change it
        if note is not None and len(note) > _MAX_NOTE:
            raise ValidationError("note", f"at most {_MAX_NOTE} characters")
        saved = await self.store.save_pricing(self._entries(entries), viewer.id, note=note)
        return convert.pricing_table(saved)

    async def list_unpriced_models(
        self, viewer: dto.Viewer, project_ids: Sequence[str] = ()
    ) -> list[dto.UnpricedModel]:
        """REQ-171.b."""
        self._admin(viewer)
        scope = await resolve_projects(self.store, viewer, project_ids)
        if not scope:
            return []
        return [convert.unpriced_model(m) for m in await self.store.list_unpriced_models(scope)]

    async def export_pricing_csv(self, viewer: dto.Viewer) -> str:
        """REQ-171.c (SHOULD): the current version in the seed's CSV columns."""
        self._admin(viewer)
        current = await self.store.get_pricing()
        day = datetime.fromtimestamp(current.meta.created_at_ns / 1e9, UTC).date().isoformat()
        return format_pricing_csv(current.entries, f"agentlens v{current.meta.version}", day)

    async def import_pricing_csv(self, viewer: dto.Viewer, csv_text: str) -> dto.PricingTable:
        """REQ-171.c (SHOULD): a new version from CSV; errors name the line and column."""
        self._mutation(viewer)
        self._full_scope(viewer)  # pricing is global: a Project-limited admin cannot change it
        if len(csv_text.encode()) > 4 << 20:
            raise ValidationError("csv", "at most 4 MiB")
        parsed = parse_pricing_csv(csv_text)
        views = [convert.pricing_table_entry(e) for e in parsed.entries]
        saved = await self.store.save_pricing(
            self._entries(views), viewer.id, source="csv", note=parsed.note[:_MAX_NOTE]
        )
        return convert.pricing_table(saved)

    async def recompute_costs(
        self,
        viewer: dto.Viewer,
        project_id: str,
        time_from: str,
        time_to: str = "now",
    ) -> int:
        """REQ-062 (SHOULD): re-price the Project's LLM calls in the range with the current
        pricing; returns how many calls were updated."""
        self._mutation(viewer, project_id)
        tr = time_range(time_from, time_to, self.clock_ns(), required=True)
        assert tr is not None  # noqa: S101 - required=True always returns a range
        pricing = await self.store.get_pricing()
        return await self.store.recompute_costs(project_id, tr, pricing)

    # ── redaction (REQ-039, REQ-172) ─────────────────────────────────────────────────────────

    async def get_redaction_rules(
        self, viewer: dto.Viewer, project_id: str
    ) -> dto.RedactionRuleSet:
        self._admin(viewer, project_id)
        settings = await self.store.get_ingest_settings(project_id)
        return convert.redaction_rules(settings.redaction)

    async def save_redaction_rules(
        self, viewer: dto.Viewer, project_id: str, rules: dto.RedactionRuleSet
    ) -> None:
        """REQ-172.a, REQ-039.d: compiled and timed against an adversarial sample at save."""
        self._mutation(viewer, project_id)
        record = convert.redaction_record(rules, viewer.id, self.clock_ns())
        validate_rule_set(record)
        await self.store.save_redaction_rules(project_id, record)

    def preview_redaction(
        self, viewer: dto.Viewer, rules: dto.RedactionRuleSet, sample: str
    ) -> str:
        """REQ-172.b (SHOULD): redacted output of the unsaved rules. Nothing is persisted or
        logged; allowed in demo mode because it changes nothing."""
        self._admin(viewer)
        return preview_redaction(convert.redaction_record(rules, viewer.id, 0), sample)

    # ── saved filters (REQ-102.a) ────────────────────────────────────────────────────────────

    async def list_saved_filters(
        self, viewer: dto.Viewer, project_id: str
    ) -> list[dto.SavedFilterView]:
        check_project(viewer, project_id)
        return [
            convert.saved_filter_view(f) for f in await self.store.list_saved_filters(project_id)
        ]

    async def save_filter(
        self,
        viewer: dto.Viewer,
        project_id: str,
        name: str,
        query: str,
        filter_id: str | None = None,
    ) -> dto.SavedFilterView:
        """Any viewer with access to the Project; the query is stored in canonical form."""
        check_project(viewer, project_id)
        if self.demo:
            raise Forbidden(DEMO_MODE)
        if filter_id is not None and not is_ulid(filter_id):
            raise ValidationError("filter_id", "expected a saved-filter ID")
        name = name.strip()
        if not name or len(name) > _MAX_FILTER_NAME:
            raise ValidationError("name", f"1 to {_MAX_FILTER_NAME} characters")
        parsed = parse_runs_query(query)
        canonical = runs_query_string(parsed.filter, parsed.sort)
        saved = await self.store.save_filter(
            r.NewSavedFilter(
                id=filter_id or new_ulid(),
                project_id=project_id,
                name=name,
                query=canonical,
                created_by=viewer.id,
            )
        )
        return convert.saved_filter_view(saved)

    async def delete_filter(self, viewer: dto.Viewer, project_id: str, filter_id: str) -> None:
        check_project(viewer, project_id)
        if self.demo:
            raise Forbidden(DEMO_MODE)
        await self.store.delete_filter(project_id, filter_id)
