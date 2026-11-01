"""Pricing, redaction rules, ingest settings and saved filters (plan task P2-08.2).

Data Model §2.4, §2.5, §2.17; REQ-060.a/c, REQ-061.b (resolution data), REQ-102.a, REQ-172.

- Pricing versions are immutable once saved: ``save_pricing`` always creates a new version
  (``max + 1``) and nothing ever updates or deletes one (REQ-060.a).
- ``get_pricing(at_ns)`` returns the newest version whose ``effective_from_ns <= at_ns``
  (REQ-060.c). Matching models against entries happens in ``ingest``, in memory.
- Saving redaction rules bumps the Project's ``settings_version`` so ingest caches drop them.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from reflex_agentlens.core.errors import Conflict, NotFound, ValidationError
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.admin import _bump, _project_pk
from reflex_agentlens.store.dialect import ops_for
from reflex_agentlens.store.records import (
    IngestSettings,
    NewSavedFilter,
    PricingEntry,
    PricingSource,
    PricingVersion,
    PricingVersionMeta,
    ProjectScope,
    RedactionRuleSet,
    SavedFilter,
    UnpricedModel,
)
from reflex_agentlens.store.schema import DEFAULT_PRESETS

__all__ = [
    "delete_filter",
    "get_ingest_settings",
    "get_or_create_secret",
    "get_pricing",
    "list_pricing_versions",
    "list_saved_filters",
    "list_unpriced_models",
    "save_filter",
    "save_pricing",
    "save_redaction_rules",
]

pv, pe, rr, sf, p = (
    s.pricing_versions,
    s.pricing_entries,
    s.redaction_rule_sets,
    s.saved_filters,
    s.projects,
)


# ── pricing ──────────────────────────────────────────────────────────────────────────────────


def _validate_entries(entries: Sequence[PricingEntry]) -> None:
    seen: set[tuple[str, str]] = set()
    for e in entries:
        if not 1 <= len(e.model_pattern) <= 200 or len(e.provider) > 64:
            raise ValidationError("pricing", "model pattern 1-200 chars, provider at most 64")
        if e.match_kind == "exact" and "*" in e.model_pattern:
            raise ValidationError("pricing", "an exact entry cannot contain '*'")
        if e.match_kind == "glob" and "*" not in e.model_pattern:
            raise ValidationError("pricing", "a glob entry needs '*'")
        prices = (
            e.input_nusd_per_mtok,
            e.output_nusd_per_mtok,
            e.cached_input_nusd_per_mtok,
            e.cache_write_nusd_per_mtok,
        )
        if any(x is not None and x < 0 for x in prices):
            raise ValidationError("pricing", "prices cannot be negative")
        key = (e.provider, e.model_pattern)
        if key in seen:
            raise ValidationError("pricing", "duplicate provider and model pattern")
        seen.add(key)


def _meta(m: Any) -> PricingVersionMeta:
    return PricingVersionMeta(
        version=m["version"],
        effective_from_ns=m["effective_from_ns"],
        source=m["source"],
        note=m["note"],
        created_at_ns=m["created_at_ns"],
        created_by=m["created_by"],
    )


def _version(conn: Connection, meta: PricingVersionMeta) -> PricingVersion:
    rows = conn.execute(
        select(pe).where(pe.c.version == meta.version).order_by(pe.c.provider, pe.c.model_pattern)
    ).mappings()
    entries = tuple(
        PricingEntry(
            model_pattern=m["model_pattern"],
            match_kind=m["match_kind"],
            input_nusd_per_mtok=m["input_nusd_per_mtok"],
            output_nusd_per_mtok=m["output_nusd_per_mtok"],
            provider=m["provider"],
            cached_input_nusd_per_mtok=m["cached_input_nusd_per_mtok"],
            cache_write_nusd_per_mtok=m["cache_write_nusd_per_mtok"],
        )
        for m in rows
    )
    return PricingVersion(meta, entries)


def save_pricing(
    conn: Connection,
    entries: Sequence[PricingEntry],
    author: str | None,
    now_ns: int,
    effective_from_ns: int | None = None,
    source: PricingSource = "editor",
    note: str | None = None,
) -> PricingVersion:
    """REQ-060.a: a new immutable version, effective from ``effective_from_ns`` (default now)."""
    _validate_entries(entries)
    if note is not None and len(note) > 500:
        raise ValidationError("note", "at most 500 characters")
    effective = now_ns if effective_from_ns is None else effective_from_ns
    if effective < 0:
        raise ValidationError("effective_from", "cannot be negative")
    version = int(conn.execute(select(func.coalesce(func.max(pv.c.version), 0))).scalar_one()) + 1
    try:
        conn.execute(
            insert(pv).values(
                version=version,
                effective_from_ns=effective,
                source=source,
                note=note,
                created_at_ns=now_ns,
                created_by=author,
            )
        )
    except IntegrityError as exc:  # another writer took the same number
        raise Conflict("pricing version") from exc
    if entries:
        conn.execute(
            insert(pe),
            [
                {
                    "version": version,
                    "provider": e.provider,
                    "model_pattern": e.model_pattern,
                    "match_kind": e.match_kind,
                    "input_nusd_per_mtok": e.input_nusd_per_mtok,
                    "output_nusd_per_mtok": e.output_nusd_per_mtok,
                    "cached_input_nusd_per_mtok": e.cached_input_nusd_per_mtok,
                    "cache_write_nusd_per_mtok": e.cache_write_nusd_per_mtok,
                }
                for e in entries
            ],
        )
    return get_pricing_version(conn, version)


def get_pricing_version(conn: Connection, version: int) -> PricingVersion:
    row = conn.execute(select(pv).where(pv.c.version == version)).mappings().first()
    if row is None:
        raise NotFound("pricing version")
    return _version(conn, _meta(row))


def list_pricing_versions(conn: Connection) -> list[PricingVersionMeta]:
    return [_meta(m) for m in conn.execute(select(pv).order_by(pv.c.version)).mappings()]


def get_pricing(conn: Connection, at_ns: int) -> PricingVersion:
    """REQ-060.c: the newest version in effect at ``at_ns``; ``NotFound`` when there is none."""
    row = (
        conn.execute(
            select(pv).where(pv.c.effective_from_ns <= at_ns).order_by(pv.c.version.desc()).limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("pricing")
    return _version(conn, _meta(row))


def list_unpriced_models(conn: Connection, project_ids: ProjectScope) -> list[UnpricedModel]:
    """REQ-171.b: models of unpriced LLM calls, most calls first (grouped in SQL)."""
    if not project_ids:
        return []
    lc = s.llm_calls
    scope = select(p.c.pk).where(p.c.id.in_(sorted(project_ids))).scalar_subquery()
    provider = func.coalesce(lc.c.provider, "")
    rows = conn.execute(
        select(provider.label("provider"), lc.c.model, func.count().label("n"),
               func.max(lc.c.start_ns).label("last"))
        .where(lc.c.project_pk.in_(scope), lc.c.unpriced.is_(True))
        .group_by(provider, lc.c.model)
    ).all()  # fmt: skip
    out = [UnpricedModel(prov, model, int(n), int(last)) for prov, model, n, last in rows]
    return sorted(out, key=lambda u: (-u.calls, u.provider, u.model))


# ── redaction rules and ingest settings ──────────────────────────────────────────────────────


def save_redaction_rules(
    conn: Connection, project_id: str, rules: RedactionRuleSet, now_ns: int
) -> None:
    """REQ-172: replace the Project's rule set and bump ``settings_version``."""
    pk = _project_pk(conn, project_id)
    if len(rules.pattern_rules) > 50 or len(rules.field_rules) > 100:
        raise ValidationError("redaction", "at most 50 pattern rules and 100 field rules")
    values = {
        "presets": rules.presets,
        "pattern_rules": rules.pattern_rules,
        "field_rules": rules.field_rules,
        "updated_at_ns": now_ns,
        "updated_by": rules.updated_by,
    }
    done = conn.execute(update(rr).where(rr.c.project_pk == pk).values(**values))
    if done.rowcount == 0:  # a Project inserted without its rule set (older tooling)
        conn.execute(insert(rr).values(project_pk=pk, **values))
    _bump(conn, pk, now_ns)


def get_ingest_settings(conn: Connection, project_id: str) -> IngestSettings:
    row = (
        conn.execute(
            select(
                p,
                rr.c.presets,
                rr.c.pattern_rules,
                rr.c.field_rules,
                rr.c.updated_at_ns.label("rules_at"),
                rr.c.updated_by.label("rules_by"),
            )
            .select_from(p.outerjoin(rr, rr.c.project_pk == p.c.pk))
            .where(p.c.id == project_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("project")
    return IngestSettings(
        project_id=project_id,
        content_storage=bool(row["content_storage"]),
        request_rate=row["request_rate"],
        request_burst=row["request_burst"],
        span_rate=row["span_rate"],
        span_burst=row["span_burst"],
        settings_version=row["settings_version"],
        redaction=RedactionRuleSet(
            presets=row["presets"] if row["presets"] is not None else json.loads(DEFAULT_PRESETS),
            pattern_rules=row["pattern_rules"] if row["pattern_rules"] is not None else [],
            field_rules=row["field_rules"] if row["field_rules"] is not None else [],
            updated_at_ns=row["rules_at"] or 0,
            updated_by=row["rules_by"],
        ),
    )


# ── saved filters (REQ-102.a) ────────────────────────────────────────────────────────────────


def _filter(m: Any) -> SavedFilter:
    return SavedFilter(
        id=m["id"],
        project_id=m["project_id"],
        name=m["name"],
        query=m["query"],
        created_by=m["created_by"],
        created_at_ns=m["created_at_ns"],
        updated_at_ns=m["updated_at_ns"],
    )


def _filters() -> Any:
    return select(p.c.id.label("project_id"), sf).select_from(sf.join(p, p.c.pk == sf.c.project_pk))


def list_saved_filters(conn: Connection, project_id: str) -> list[SavedFilter]:
    _project_pk(conn, project_id)
    rows = conn.execute(_filters().where(p.c.id == project_id).order_by(sf.c.name)).mappings()
    return [_filter(m) for m in rows]


def save_filter(conn: Connection, f: NewSavedFilter, now_ns: int) -> SavedFilter:
    """Create, or update by ``id`` within the same Project; names are unique per Project."""
    if not 1 <= len(f.name.strip()) <= 120:
        raise ValidationError("name", "must be 1 to 120 characters")
    if len(f.query) > 4096:
        raise ValidationError("query", "at most 4096 characters")
    pk = _project_pk(conn, f.project_id)
    clash = conn.execute(
        select(sf.c.id).where(sf.c.project_pk == pk, sf.c.name == f.name, sf.c.id != f.id)
    ).first()
    if clash is not None:
        raise Conflict("saved filter")
    existing = conn.execute(select(sf.c.project_pk).where(sf.c.id == f.id)).first()
    if existing is not None:
        if existing.project_pk != pk:
            raise Conflict("saved filter")
        conn.execute(
            update(sf)
            .where(sf.c.id == f.id)
            .values(name=f.name, query=f.query, updated_at_ns=now_ns)
        )
    else:
        conn.execute(
            insert(sf).values(
                id=f.id, project_pk=pk, name=f.name, query=f.query, created_by=f.created_by,
                created_at_ns=now_ns, updated_at_ns=now_ns,
            )
        )  # fmt: skip
    row = conn.execute(_filters().where(sf.c.id == f.id)).mappings().one()
    return _filter(row)


def delete_filter(conn: Connection, project_id: str, filter_id: str) -> None:
    """Only a filter of ``project_id``; one of another Project is ``NotFound`` (API v1.3.1)."""
    pk = _project_pk(conn, project_id)
    if conn.execute(delete(sf).where(sf.c.id == filter_id, sf.c.project_pk == pk)).rowcount == 0:
        raise NotFound("saved filter")


# ── app_meta secrets (Data Model §2.1, Analyze A-08) ─────────────────────────────────────────


def get_or_create_secret(conn: Connection, name: str) -> str:
    """32 random bytes as hex, created once with insert-or-ignore so every process agrees."""
    t = s.app_meta
    ops = ops_for(conn.dialect.name)
    stmt = ops.insert(t).values(key=name, value=secrets.token_hex(32))
    conn.execute(stmt.on_conflict_do_nothing(index_elements=[t.c.key]))
    return str(conn.execute(select(t.c.value).where(t.c.key == name)).scalar_one())
