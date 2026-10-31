"""Projects and API key records (plan task P2-08.1; Data Model §2.2-2.4, Q14).

- Slugs are unique (``Conflict``) and must match the Data Model §2.2 pattern; the query layer
  validates them first, this is the last line of defence.
- A new Project gets its default redaction rule set row (Data Model §2.4) in the same transaction.
- ``settings_version`` is bumped by any change to the Project row or its keys, so ingest caches
  notice (REQ-006.c).
- API keys are stored as salt + SHA-256 hash only (REQ-320.a); the plaintext secret never reaches
  this module. ``touch_api_key`` writes at most once per minute per key.
- Deleting a Project removes its Runs first (cascading to spans, derived rows and search rows),
  then metric points and Sessions, then the Project (cascading Sources, resources, keys, rules
  and saved filters), so no ``RESTRICT`` foreign key is hit.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Connection, delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from reflex_agentlens.core.errors import Conflict, NotFound, ValidationError
from reflex_agentlens.store import schema as s
from reflex_agentlens.store.records import (
    ApiKeyRecord,
    NewApiKeyRecord,
    NewProject,
    Project,
    ProjectPatch,
)

__all__ = [
    "SLUG",
    "TOUCH_INTERVAL_NS",
    "create_api_key",
    "create_project",
    "delete_project",
    "list_api_keys",
    "list_projects",
    "resolve_api_key",
    "revoke_api_key",
    "touch_api_key",
    "update_project",
]

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
TOUCH_INTERVAL_NS = 60 * 1_000_000_000
p, k = s.projects, s.api_keys
_PROJECT_COLS = [c for c in p.columns if c.name != "pk"]


def _project(m: Any) -> Project:
    return Project(
        id=m["id"],
        slug=m["slug"],
        name=m["name"],
        content_storage=bool(m["content_storage"]),
        retention_max_age_s=m["retention_max_age_s"],
        request_rate=m["request_rate"],
        request_burst=m["request_burst"],
        span_rate=m["span_rate"],
        span_burst=m["span_burst"],
        settings_version=m["settings_version"],
        created_at_ns=m["created_at_ns"],
        updated_at_ns=m["updated_at_ns"],
        updated_by=m["updated_by"],
    )


def _get_project(conn: Connection, project_id: str) -> Project:
    row = conn.execute(select(*_PROJECT_COLS).where(p.c.id == project_id)).mappings().first()
    if row is None:
        raise NotFound("project")
    return _project(row)


def _project_pk(conn: Connection, project_id: str) -> int:
    pk = conn.execute(select(p.c.pk).where(p.c.id == project_id)).scalar()
    if pk is None:
        raise NotFound("project")
    return int(pk)


def _check_name(name: str) -> None:
    if not 1 <= len(name.strip()) <= 120:
        raise ValidationError("name", "must be 1 to 120 characters")


# ── projects ─────────────────────────────────────────────────────────────────────────────────


def list_projects(conn: Connection) -> list[Project]:
    rows = conn.execute(select(*_PROJECT_COLS).order_by(p.c.slug)).mappings()
    return [_project(m) for m in rows]


def create_project(conn: Connection, new: NewProject, now_ns: int) -> Project:
    """REQ-170.a: a Project with default settings and its default redaction rule set."""
    if not SLUG.match(new.slug):
        raise ValidationError("slug", "lowercase letters, digits and dashes, 3 to 40 characters")
    _check_name(new.name)
    if new.retention_max_age_s < 0:
        raise ValidationError("retention", "must be zero (forever) or positive")
    exists = conn.execute(select(p.c.pk).where(or_(p.c.slug == new.slug, p.c.id == new.id))).first()
    if exists is not None:
        raise Conflict("project")
    try:
        pk = conn.execute(
            insert(p)
            .values(
                id=new.id,
                slug=new.slug,
                name=new.name,
                content_storage=new.content_storage,
                retention_max_age_s=new.retention_max_age_s,
                created_at_ns=now_ns,
                updated_at_ns=now_ns,
                updated_by=new.created_by,
            )
            .returning(p.c.pk)
        ).scalar_one()
    except IntegrityError as exc:  # a concurrent creator won the unique index
        raise Conflict("project") from exc
    conn.execute(
        insert(s.redaction_rule_sets).values(
            project_pk=pk, updated_at_ns=now_ns, updated_by=new.created_by
        )
    )
    return _get_project(conn, new.id)


def update_project(conn: Connection, project_id: str, patch: ProjectPatch, now_ns: int) -> Project:
    """Apply the non-``None`` fields and bump ``settings_version`` (REQ-173, REQ-006.c)."""
    values: dict[str, Any] = {
        f: getattr(patch, f)
        for f in (
            "name", "content_storage", "retention_max_age_s",
            "request_rate", "request_burst", "span_rate", "span_burst",
        )
        if getattr(patch, f) is not None
    }  # fmt: skip
    if "name" in values:
        _check_name(values["name"])
    if values.get("retention_max_age_s", 0) < 0:
        raise ValidationError("retention", "must be zero (forever) or positive")
    for f in ("request_rate", "request_burst", "span_rate", "span_burst"):
        if f in values and values[f] <= 0:
            raise ValidationError(f, "must be positive")
    result = conn.execute(
        update(p)
        .where(p.c.id == project_id)
        .values(
            **values,
            settings_version=p.c.settings_version + 1,
            updated_at_ns=now_ns,
            updated_by=patch.updated_by,
        )
    )
    if result.rowcount == 0:
        raise NotFound("project")
    return _get_project(conn, project_id)


def delete_project(conn: Connection, project_id: str) -> None:
    """REQ-170.d: remove the Project and every row that belongs to it."""
    pk = _project_pk(conn, project_id)
    conn.execute(delete(s.runs).where(s.runs.c.project_pk == pk))
    conn.execute(delete(s.metric_points).where(s.metric_points.c.project_pk == pk))
    conn.execute(delete(s.sessions).where(s.sessions.c.project_pk == pk))
    conn.execute(delete(p).where(p.c.pk == pk))


# ── API keys ─────────────────────────────────────────────────────────────────────────────────


def _key_select() -> Any:
    return select(p.c.id.label("project_id"), k).select_from(k.join(p, p.c.pk == k.c.project_pk))


def _key(m: Any) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=m["key_id"],
        project_id=m["project_id"],
        label=m["label"],
        salt=bytes(m["salt"]),
        secret_hash=bytes(m["secret_hash"]),
        created_at_ns=m["created_at_ns"],
        created_by=m["created_by"],
        last_used_at_ns=m["last_used_at_ns"],
        revoked_at_ns=m["revoked_at_ns"],
    )


def _bump(conn: Connection, project_pk: int, now_ns: int) -> None:
    conn.execute(
        update(p)
        .where(p.c.pk == project_pk)
        .values(settings_version=p.c.settings_version + 1, updated_at_ns=now_ns)
    )


def create_api_key(conn: Connection, rec: NewApiKeyRecord) -> ApiKeyRecord:
    """REQ-170.b: store the key's public ID, salt and hash (never the secret, REQ-320.a)."""
    if len(rec.salt) != 16 or len(rec.secret_hash) != 32:
        raise ValidationError("api_key", "salt must be 16 bytes and hash 32 bytes")
    if len(rec.label) > 120:
        raise ValidationError("label", "at most 120 characters")
    project_pk = _project_pk(conn, rec.project_id)
    if conn.execute(select(k.c.pk).where(k.c.key_id == rec.key_id)).first() is not None:
        raise Conflict("api key")
    conn.execute(
        insert(k).values(
            key_id=rec.key_id,
            project_pk=project_pk,
            label=rec.label,
            salt=rec.salt,
            secret_hash=rec.secret_hash,
            created_at_ns=rec.created_at_ns,
            created_by=rec.created_by,
        )
    )
    _bump(conn, project_pk, rec.created_at_ns)
    found = resolve_api_key(conn, rec.key_id)
    if found is None:  # pragma: no cover - the row was inserted in this transaction
        raise NotFound("api key")
    return found


def list_api_keys(conn: Connection, project_id: str) -> list[ApiKeyRecord]:
    _project_pk(conn, project_id)
    rows = conn.execute(
        _key_select().where(p.c.id == project_id).order_by(k.c.created_at_ns, k.c.key_id)
    ).mappings()
    return [_key(m) for m in rows]


def resolve_api_key(conn: Connection, key_id: str) -> ApiKeyRecord | None:
    """Q14: lookup by public key ID; revoked keys are returned so callers can tell them apart."""
    row = conn.execute(_key_select().where(k.c.key_id == key_id)).mappings().first()
    return None if row is None else _key(row)


def touch_api_key(conn: Connection, key_id: str, at_ns: int) -> None:
    """Record use, at most once per ``TOUCH_INTERVAL_NS`` per key (Data Model §2.3)."""
    conn.execute(
        update(k)
        .where(
            k.c.key_id == key_id,
            or_(k.c.last_used_at_ns.is_(None), k.c.last_used_at_ns <= at_ns - TOUCH_INTERVAL_NS),
        )
        .values(last_used_at_ns=at_ns)
    )


def revoke_api_key(conn: Connection, key_id: str, at_ns: int) -> str:
    """REQ-170.c: terminal; revoking twice keeps the first time. Returns the Project ID."""
    row = conn.execute(
        select(k.c.project_pk, k.c.revoked_at_ns, p.c.id)
        .select_from(k.join(p, p.c.pk == k.c.project_pk))
        .where(k.c.key_id == key_id)
    ).first()
    if row is None:
        raise NotFound("api key")
    if row.revoked_at_ns is None:
        conn.execute(update(k).where(k.c.key_id == key_id).values(revoked_at_ns=at_ns))
        _bump(conn, row.project_pk, at_ns)
    return str(row.id)
