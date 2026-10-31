"""Database schema: Data Model v1.1 §2.1-2.18 as SQLAlchemy 2.0 Core (plan tasks P2-01.1, P2-01.2).

One :data:`metadata` serves SQLite and PostgreSQL. Dialect differences of Data Model §2.0 are
expressed with ``with_variant`` (types) and ``ddl_if`` (constraints and indexes that exist on one
backend only). ``span_search`` (§2.15) differs structurally per backend — an FTS5 virtual table on
SQLite, a regular table with a ``tsvector`` on PostgreSQL — and is created by
:func:`create_search`; :func:`create_all` creates everything.

This module describes the **current** schema. Migrations never import it: ``0001_initial`` uses a
frozen snapshot (``migrations/versions/_schema_0001.py``), and a test fails when this module and
the migration head drift apart.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

__all__ = [
    "SEARCH_TABLE",
    "create_all",
    "create_search",
    "metadata",
]

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)
SEARCH_TABLE = "span_search"

# ── §2.0 physical type mapping ───────────────────────────────────────────────────────────────

BIGINT = BigInteger().with_variant(Integer, "sqlite")  # SQLite INTEGER is 64-bit anyway
JSONT = JSON().with_variant(JSONB(), "postgresql")


def _pk() -> Column[Any]:
    """`PK`: SQLite ``INTEGER PRIMARY KEY`` (rowid alias); PG ``BIGINT … AS IDENTITY``."""
    return Column("pk", BIGINT, Identity(always=True), primary_key=True)


def _text(name: str, n: int, table: str, **kw: Any) -> tuple[Column[Any], CheckConstraint]:
    """`TEXT(n)`: PG ``VARCHAR(n)``; SQLite ``TEXT`` + length CHECK (SQLite ignores VARCHAR(n))."""
    ck = CheckConstraint(f"length({name}) <= {n}", name=f"ck_{table}_{name}_len")
    return Column(name, String(n), **kw), ck.ddl_if(dialect="sqlite")


def _bytes(name: str, n: int, table: str, **kw: Any) -> tuple[Column[Any], CheckConstraint]:
    """`BYTES(n)`: BLOB/BYTEA + CHECK on the exact length (``length()`` counts bytes on both)."""
    ck = CheckConstraint(f"length({name}) = {n}", name=f"ck_{table}_{name}_len")
    return Column(name, LargeBinary, **kw), ck


def _bool(name: str, table: str, **kw: Any) -> Column[Any]:
    """`BOOL`: PG ``BOOLEAN``; SQLite ``INTEGER`` + ``CHECK (col IN (0,1))``."""
    return Column(name, Boolean(create_constraint=True, name=f"ck_{table}_{name}_bool"), **kw)


def _json(name: str, table: str, default: str | None) -> tuple[Column[Any], CheckConstraint]:
    """`JSON`: PG ``JSONB``; SQLite ``TEXT`` + ``CHECK (json_valid(col))``."""
    kw: dict[str, Any] = {"nullable": default is None}
    if default is not None:
        # A plain str is quoted; text() would parse ":true" as a bind parameter.
        kw["server_default"] = default
    ck = CheckConstraint(f"json_valid({name})", name=f"ck_{table}_{name}_json")
    return Column(name, JSONT, **kw), ck.ddl_if(dialect="sqlite")


def _enum(name: str, values: tuple[str, ...], table: str) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{name} IN ({allowed})", name=f"ck_{table}_{name}")


def _table(name: str, *items: Any) -> Table:
    flat: list[Any] = []
    for item in items:
        flat.extend(item if isinstance(item, tuple) else (item,))
    return Table(name, metadata, *flat)


def _fk(target: str, ondelete: str | None = "CASCADE") -> ForeignKey:
    return ForeignKey(target, ondelete=ondelete)


def _now_ns(name: str) -> Column[Any]:
    return Column(name, BIGINT, nullable=False)


# ── §2.1 app_meta ────────────────────────────────────────────────────────────────────────────

app_meta = _table(
    "app_meta",
    _text("key", 64, "app_meta", primary_key=True),
    Column("value", Text, nullable=False),
)

# ── §2.2 projects ────────────────────────────────────────────────────────────────────────────

projects = _table(
    "projects",
    _pk(),
    _text("id", 26, "projects", nullable=False),
    _text("slug", 40, "projects", nullable=False),
    _text("name", 120, "projects", nullable=False),
    _bool("content_storage", "projects", nullable=False, server_default=text("true")),
    Column("retention_max_age_s", BIGINT, nullable=False, server_default=text("2592000")),
    Column("request_rate", Integer, nullable=False, server_default=text("50")),
    Column("request_burst", Integer, nullable=False, server_default=text("100")),
    Column("span_rate", Integer, nullable=False, server_default=text("20000")),
    Column("span_burst", Integer, nullable=False, server_default=text("40000")),
    Column("settings_version", Integer, nullable=False, server_default=text("1")),
    _now_ns("created_at_ns"),
    _now_ns("updated_at_ns"),
    _text("updated_by", 128, "projects", nullable=True),
    CheckConstraint("retention_max_age_s >= 0", name="ck_projects_retention"),
    CheckConstraint(
        "request_rate > 0 AND request_burst > 0 AND span_rate > 0 AND span_burst > 0",
        name="ck_projects_limits",
    ),
)
Index("uq_projects_id", projects.c.id, unique=True)
Index("uq_projects_slug", projects.c.slug, unique=True)

# ── §2.3 api_keys ────────────────────────────────────────────────────────────────────────────

api_keys = _table(
    "api_keys",
    _pk(),
    _text("key_id", 8, "api_keys", nullable=False),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    _text("label", 120, "api_keys", nullable=False, server_default=text("''")),
    _bytes("salt", 16, "api_keys", nullable=False),
    _bytes("secret_hash", 32, "api_keys", nullable=False),
    _now_ns("created_at_ns"),
    _text("created_by", 128, "api_keys", nullable=True),
    Column("last_used_at_ns", BIGINT, nullable=True),
    Column("revoked_at_ns", BIGINT, nullable=True),
)
Index("uq_api_keys_key_id", api_keys.c.key_id, unique=True)
Index("ix_api_keys_project", api_keys.c.project_pk)

# ── §2.4 redaction_rule_sets ─────────────────────────────────────────────────────────────────

DEFAULT_PRESETS = '{"secrets":true,"email":false,"phone":false,"credit_card":false}'
redaction_rule_sets = _table(
    "redaction_rule_sets",
    Column("project_pk", BIGINT, _fk("projects.pk"), primary_key=True, autoincrement=False),
    _json("presets", "redaction_rule_sets", DEFAULT_PRESETS),
    _json("pattern_rules", "redaction_rule_sets", "[]"),
    _json("field_rules", "redaction_rule_sets", "[]"),
    _now_ns("updated_at_ns"),
    _text("updated_by", 128, "redaction_rule_sets", nullable=True),
)

# ── §2.5 pricing_versions, pricing_entries ───────────────────────────────────────────────────

pricing_versions = _table(
    "pricing_versions",
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("effective_from_ns", BIGINT, nullable=False),
    _text("source", 16, "pricing_versions", nullable=False),
    _enum("source", ("bundled", "editor", "csv"), "pricing_versions"),
    _text("note", 500, "pricing_versions", nullable=True),
    _now_ns("created_at_ns"),
    _text("created_by", 128, "pricing_versions", nullable=True),
)

pricing_entries = _table(
    "pricing_entries",
    _pk(),
    Column("version", Integer, _fk("pricing_versions.version", "RESTRICT"), nullable=False),
    _text("provider", 64, "pricing_entries", nullable=False, server_default=text("''")),
    _text("model_pattern", 200, "pricing_entries", nullable=False),
    _text("match_kind", 8, "pricing_entries", nullable=False),
    _enum("match_kind", ("exact", "glob"), "pricing_entries"),
    Column("input_nusd_per_mtok", BIGINT, nullable=False),
    Column("output_nusd_per_mtok", BIGINT, nullable=False),
    Column("cached_input_nusd_per_mtok", BIGINT, nullable=True),
    Column("cache_write_nusd_per_mtok", BIGINT, nullable=True),
    CheckConstraint(
        "input_nusd_per_mtok >= 0 AND output_nusd_per_mtok >= 0"
        " AND (cached_input_nusd_per_mtok IS NULL OR cached_input_nusd_per_mtok >= 0)"
        " AND (cache_write_nusd_per_mtok IS NULL OR cache_write_nusd_per_mtok >= 0)",
        name="ck_pricing_entries_prices",
    ),
)
Index(
    "uq_pricing_entry",
    pricing_entries.c.version,
    pricing_entries.c.provider,
    pricing_entries.c.model_pattern,
    unique=True,
)

# ── §2.6 sources ─────────────────────────────────────────────────────────────────────────────

sources = _table(
    "sources",
    _pk(),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    _text("service_namespace", 128, "sources", nullable=False, server_default=text("''")),
    _text("service_name", 256, "sources", nullable=False),
    Column("first_seen_ns", BIGINT, nullable=False),
    Column("last_seen_ns", BIGINT, nullable=False),
)
Index(
    "uq_sources",
    sources.c.project_pk,
    sources.c.service_namespace,
    sources.c.service_name,
    unique=True,
)

# ── §2.7 resources ───────────────────────────────────────────────────────────────────────────

resources = _table(
    "resources",
    _pk(),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    _bytes("attrs_hash", 16, "resources", nullable=False),
    _json("attributes", "resources", "{}"),
    _text("schema_url", 512, "resources", nullable=True),
)
Index("uq_resources", resources.c.project_pk, resources.c.attrs_hash, unique=True)

# ── §2.8 sessions ────────────────────────────────────────────────────────────────────────────

sessions = _table(
    "sessions",
    _pk(),
    _text("id", 26, "sessions", nullable=False),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    _text("external_id", 512, "sessions", nullable=False),
    Column("first_ns", BIGINT, nullable=False),
    Column("last_ns", BIGINT, nullable=False),
    Column("run_count", Integer, nullable=False, server_default=text("0")),
    Column("input_tokens", BIGINT, nullable=False, server_default=text("0")),
    Column("output_tokens", BIGINT, nullable=False, server_default=text("0")),
    Column("cached_input_tokens", BIGINT, nullable=False, server_default=text("0")),
    Column("cost_nusd", BIGINT, nullable=False, server_default=text("0")),
    _bool("includes_unpriced", "sessions", nullable=False, server_default=text("false")),
    _now_ns("updated_at_ns"),
)
Index("uq_sessions_id", sessions.c.id, unique=True)
Index("uq_sessions_ext", sessions.c.project_pk, sessions.c.external_id, unique=True)
Index("ix_sessions_project_last", sessions.c.project_pk, sessions.c.last_ns.desc(), sessions.c.pk)

# ── §2.9 runs ────────────────────────────────────────────────────────────────────────────────

RUN_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
RUN_COUNT_COLUMNS = (
    "unpriced_calls",
    "llm_calls",
    "tool_calls",
    "error_spans",
    "span_count",
    "missing_parents",
)
runs = _table(
    "runs",
    _pk(),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    _bytes("trace_id", 16, "runs", nullable=False),
    Column("source_pk", BIGINT, _fk("sources.pk", "RESTRICT"), nullable=True),
    Column("source_count", SmallInteger, nullable=False, server_default=text("1")),
    Column("session_pk", BIGINT, _fk("sessions.pk", "SET NULL"), nullable=True),
    _text("status", 12, "runs", nullable=False, server_default=text("'in_progress'")),
    _enum("status", ("in_progress", "ok", "error", "incomplete"), "runs"),
    Column("start_ns", BIGINT, nullable=False),
    Column("end_ns", BIGINT, nullable=True),
    Column("duration_ns", BIGINT, nullable=True),
    Column("last_span_ns", BIGINT, nullable=False),
    Column("root_span_pk", BIGINT, nullable=True),
    _text("root_name", 256, "runs", nullable=True),
    _text("agent_name", 256, "runs", nullable=True),
    _bool("is_genai", "runs", nullable=False, server_default=text("false")),
    _json("models", "runs", "[]"),
    *[Column(c, BIGINT, nullable=False, server_default=text("0")) for c in RUN_TOKEN_COLUMNS],
    _text("usage_source", 8, "runs", nullable=False, server_default=text("'none'")),
    _enum("usage_source", ("llm", "agent", "none"), "runs"),
    Column("cost_nusd", BIGINT, nullable=True),
    *[Column(c, Integer, nullable=False, server_default=text("0")) for c in RUN_COUNT_COLUMNS],
    Column("bytes_est", BIGINT, nullable=False, server_default=text("0")),
    _now_ns("updated_at_ns"),
)
Index("uq_runs_trace", runs.c.project_pk, runs.c.trace_id, unique=True)
Index("ix_runs_project_start", runs.c.project_pk, runs.c.start_ns.desc(), runs.c.pk.desc())
Index("ix_runs_project_status_start", runs.c.project_pk, runs.c.status, runs.c.start_ns.desc())
Index("ix_runs_project_source_start", runs.c.project_pk, runs.c.source_pk, runs.c.start_ns.desc())
Index("ix_runs_session_start", runs.c.session_pk, runs.c.start_ns)
Index("ix_runs_project_last", runs.c.project_pk, runs.c.last_span_ns)
Index(
    "ix_runs_inprogress",
    runs.c.last_span_ns,
    postgresql_where=text("status = 'in_progress'"),
    sqlite_where=text("status = 'in_progress'"),
)

# ── §2.10 spans ──────────────────────────────────────────────────────────────────────────────

SPAN_CATEGORIES = ("llm", "tool", "agent", "approval", "generic_genai", "generic")
spans = _table(
    "spans",
    _pk(),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    Column("run_pk", BIGINT, _fk("runs.pk"), nullable=False),
    _bytes("span_id", 8, "spans", nullable=False),
    Column("parent_span_id", LargeBinary, nullable=True),
    CheckConstraint(
        "parent_span_id IS NULL OR length(parent_span_id) = 8", name="ck_spans_parent_span_id_len"
    ),
    Column("source_pk", BIGINT, _fk("sources.pk", "RESTRICT"), nullable=False),
    Column("resource_pk", BIGINT, _fk("resources.pk", "RESTRICT"), nullable=False),
    _text("name", 1024, "spans", nullable=False),
    Column("otel_kind", SmallInteger, nullable=False, server_default=text("0")),
    _text("category", 14, "spans", nullable=False, server_default=text("'generic'")),
    _enum("category", SPAN_CATEGORIES, "spans"),
    _bool("is_genai", "spans", nullable=False, server_default=text("false")),
    Column("start_ns", BIGINT, nullable=False),
    Column("end_ns", BIGINT, nullable=False),
    CheckConstraint("end_ns >= start_ns", name="ck_spans_time"),
    Column("status_code", SmallInteger, nullable=False, server_default=text("0")),
    Column("status_message", Text, nullable=True),
    _text("scope_name", 256, "spans", nullable=True),
    _text("scope_version", 64, "spans", nullable=True),
    _json("attributes", "spans", "{}"),
    _json("events", "spans", "[]"),
    _json("links", "spans", "[]"),
    _json("dropped_counts", "spans", "{}"),
    _json("dropped_keys", "spans", "[]"),
    _text("agent_name", 256, "spans", nullable=True),
    _text("session_ext", 512, "spans", nullable=True),
    Column("agent_usage_input", BIGINT, nullable=True),
    Column("agent_usage_output", BIGINT, nullable=True),
    Column("flags", SmallInteger, nullable=False, server_default=text("0")),
    Column("mapping_version", SmallInteger, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("received_at_ns", BIGINT, nullable=False),
)
Index("uq_spans_run_span", spans.c.run_pk, spans.c.span_id, unique=True)
Index(
    "ix_spans_root_attrs",
    spans.c.attributes,
    postgresql_using="gin",
    postgresql_ops={"attributes": "jsonb_ops"},
    postgresql_where=text("parent_span_id IS NULL"),
).ddl_if(dialect="postgresql")


def _derived(name: str, *items: Any) -> Table:
    """Per-span derived tables: ``span_pk`` PK/FK, ``run_pk`` FK, denormalized ``project_pk``."""
    return _table(
        name,
        Column("span_pk", BIGINT, _fk("spans.pk"), primary_key=True, autoincrement=False),
        Column("run_pk", BIGINT, _fk("runs.pk"), nullable=False),
        Column("project_pk", BIGINT, nullable=False),
        *items,
    )


# ── §2.11 llm_calls ──────────────────────────────────────────────────────────────────────────

LLM_COVERING = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cost_nusd",
    "duration_ns",
    "operation",
)
llm_calls = _derived(
    "llm_calls",
    Column("start_ns", BIGINT, nullable=False),
    Column("duration_ns", BIGINT, nullable=False),
    _text("operation", 32, "llm_calls", nullable=False),
    _text("provider", 64, "llm_calls", nullable=True),
    _text("request_model", 200, "llm_calls", nullable=True),
    _text("response_model", 200, "llm_calls", nullable=True),
    _text("model", 200, "llm_calls", nullable=False, server_default=text("'unknown'")),
    *[Column(c, BIGINT, nullable=True) for c in (*RUN_TOKEN_COLUMNS, "cost_nusd")],
    _bool("unpriced", "llm_calls", nullable=False),
    Column("pricing_version", Integer, _fk("pricing_versions.version", "RESTRICT"), nullable=True),
    _text("status", 5, "llm_calls", nullable=False, server_default=text("'ok'")),
    _enum("status", ("ok", "error"), "llm_calls"),
    _text("error_type", 128, "llm_calls", nullable=True),
    _json("finish_reasons", "llm_calls", "[]"),
    _json("params", "llm_calls", "{}"),
    _bool("content_dropped", "llm_calls", nullable=False, server_default=text("false")),
)
Index(
    "ix_llm_project_start",
    llm_calls.c.project_pk,
    llm_calls.c.start_ns,
    llm_calls.c.model,
    postgresql_include=list(LLM_COVERING),
).ddl_if(dialect="postgresql")
Index(  # SQLite has no INCLUDE: the covering columns are appended to the key (§2.11)
    "ix_llm_project_start",
    llm_calls.c.project_pk,
    llm_calls.c.start_ns,
    llm_calls.c.model,
    *[llm_calls.c[c] for c in LLM_COVERING],
).ddl_if(dialect="sqlite")
Index("ix_llm_project_model_run", llm_calls.c.project_pk, llm_calls.c.model, llm_calls.c.run_pk)
Index("ix_llm_run", llm_calls.c.run_pk)

# ── §2.12 tool_calls ─────────────────────────────────────────────────────────────────────────

tool_calls = _derived(
    "tool_calls",
    Column("start_ns", BIGINT, nullable=False),
    Column("duration_ns", BIGINT, nullable=False),
    _text("tool_name", 256, "tool_calls", nullable=False, server_default=text("'unknown'")),
    _text("tool_call_id", 256, "tool_calls", nullable=True),
    _text("tool_type", 32, "tool_calls", nullable=True),
    _text("status", 5, "tool_calls", nullable=False),
    _enum("status", ("ok", "error"), "tool_calls"),
    _text("error_type", 128, "tool_calls", nullable=True),
    _bool("content_dropped", "tool_calls", nullable=False, server_default=text("false")),
)
Index(
    "ix_tool_project_start",
    tool_calls.c.project_pk,
    tool_calls.c.start_ns,
    tool_calls.c.tool_name,
    tool_calls.c.status,
    tool_calls.c.duration_ns,
)
Index(
    "ix_tool_project_name_run", tool_calls.c.project_pk, tool_calls.c.tool_name, tool_calls.c.run_pk
)
Index("ix_tool_run", tool_calls.c.run_pk)

# ── §2.13 approvals ──────────────────────────────────────────────────────────────────────────

approvals = _derived(
    "approvals",
    _text("approval_key", 256, "approvals", nullable=True),
    _text("kind", 12, "approvals", nullable=False),
    _enum("kind", ("approval", "deferral", "elicitation"), "approvals"),
    _text("decision", 10, "approvals", nullable=False),
    _enum("decision", ("approved", "rejected", "edited", "timeout", "pending"), "approvals"),
    _text("approver", 256, "approvals", nullable=True),
    Column("requested_ns", BIGINT, nullable=False),
    Column("decided_ns", BIGINT, nullable=True),
    Column("wait_ns", BIGINT, nullable=True),
    _text("tool_call_id", 256, "approvals", nullable=True),
    _text("tool_name", 256, "approvals", nullable=True),
    _text("node_name", 256, "approvals", nullable=True),
)
Index(
    "ix_appr_project_requested",
    approvals.c.project_pk,
    approvals.c.requested_ns,
    approvals.c.decision,
    approvals.c.wait_ns,
)
Index("ix_appr_run", approvals.c.run_pk)

# ── §2.14 graph_nodes ────────────────────────────────────────────────────────────────────────

graph_nodes = _derived(
    "graph_nodes",
    _text("node_key", 256, "graph_nodes", nullable=False),
    _text("node_name", 256, "graph_nodes", nullable=False),
    _text("node_kind", 8, "graph_nodes", nullable=False),
    _enum("node_kind", ("agent", "workflow", "node"), "graph_nodes"),
    _text("explicit_parent_key", 256, "graph_nodes", nullable=True),
    Column("step", Integer, nullable=True),
    _text("agent_name", 256, "graph_nodes", nullable=True),
    Column("start_ns", BIGINT, nullable=False),
    Column("end_ns", BIGINT, nullable=False),
    _text("status", 5, "graph_nodes", nullable=False),
    _enum("status", ("ok", "error"), "graph_nodes"),
)
Index(
    "ix_gn_project_start",
    graph_nodes.c.project_pk,
    graph_nodes.c.start_ns,
    graph_nodes.c.node_name,
    graph_nodes.c.end_ns,
)
Index("ix_gn_run", graph_nodes.c.run_pk)

# ── §2.16 metric_points ──────────────────────────────────────────────────────────────────────

metric_points = _table(
    "metric_points",
    _pk(),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    Column("source_pk", BIGINT, _fk("sources.pk", "RESTRICT"), nullable=False),
    _text("name", 128, "metric_points", nullable=False),
    _text("kind", 9, "metric_points", nullable=False),
    _enum("kind", ("sum", "gauge", "histogram"), "metric_points"),
    _text("unit", 32, "metric_points", nullable=True),
    _bool("is_monotonic", "metric_points", nullable=True),
    Column("temporality", SmallInteger, nullable=True),
    Column("start_ns", BIGINT, nullable=False),
    Column("time_ns", BIGINT, nullable=False),
    _bytes("attrs_hash", 16, "metric_points", nullable=False),
    _json("attributes", "metric_points", "{}"),
    _text("model", 200, "metric_points", nullable=True),
    _text("token_type", 16, "metric_points", nullable=True),
    Column("value_int", BIGINT, nullable=True),
    Column("value_double", Double, nullable=True),
    Column("count", BIGINT, nullable=True),
    Column("sum", Double, nullable=True),
    _json("bucket_counts", "metric_points", None),
    _json("explicit_bounds", "metric_points", None),
)
Index(
    "uq_metric_point",
    metric_points.c.project_pk,
    metric_points.c.source_pk,
    metric_points.c.name,
    metric_points.c.attrs_hash,
    metric_points.c.start_ns,
    metric_points.c.time_ns,
    unique=True,
)
Index(
    "ix_mp_project_name_time",
    metric_points.c.project_pk,
    metric_points.c.name,
    metric_points.c.time_ns,
)

# ── §2.17 saved_filters ──────────────────────────────────────────────────────────────────────

saved_filters = _table(
    "saved_filters",
    _pk(),
    _text("id", 26, "saved_filters", nullable=False),
    Column("project_pk", BIGINT, _fk("projects.pk"), nullable=False),
    _text("name", 120, "saved_filters", nullable=False),
    _text("query", 4096, "saved_filters", nullable=False),
    _text("created_by", 128, "saved_filters", nullable=True),
    _now_ns("created_at_ns"),
    _now_ns("updated_at_ns"),
)
Index("uq_saved_filters_id", saved_filters.c.id, unique=True)
Index("uq_saved_filters_name", saved_filters.c.project_pk, saved_filters.c.name, unique=True)

# ── §2.18 purge_log ──────────────────────────────────────────────────────────────────────────

purge_log = _table(
    "purge_log",
    _pk(),
    _text("trigger", 9, "purge_log", nullable=False),
    _enum("trigger", ("scheduled", "manual"), "purge_log"),
    Column("started_ns", BIGINT, nullable=False),
    Column("finished_ns", BIGINT, nullable=True),
    Column("deleted_traces_age", Integer, nullable=False, server_default=text("0")),
    Column("deleted_traces_size", Integer, nullable=False, server_default=text("0")),
    Column("deleted_bytes_est", BIGINT, nullable=False, server_default=text("0")),
    _text("error_class", 128, "purge_log", nullable=True),
)

# ── §2.15 span_search (per backend) ──────────────────────────────────────────────────────────

pg_search_metadata = MetaData(naming_convention=NAMING)
span_search_pg = Table(
    SEARCH_TABLE,
    pg_search_metadata,
    Column("span_pk", BigInteger, primary_key=True, autoincrement=False),
    Column("run_pk", BigInteger, nullable=False),
    Column("project_pk", BigInteger, nullable=False),
    Column("tsv", TSVECTOR, nullable=False),
)
Index("ix_search_tsv", span_search_pg.c.tsv, postgresql_using="gin")
Index("ix_search_run", span_search_pg.c.run_pk)

PG_SEARCH_FK = (
    "ALTER TABLE span_search ADD CONSTRAINT fk_span_search_span_pk_spans "
    "FOREIGN KEY (span_pk) REFERENCES spans (pk) ON DELETE CASCADE"
)
SQLITE_SEARCH_DDL = (
    "CREATE VIRTUAL TABLE span_search USING fts5(doc, content='', contentless_delete=1, "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER trg_spans_search_delete AFTER DELETE ON spans "
    "BEGIN DELETE FROM span_search WHERE rowid = old.pk; END",
)


def create_search(conn: Connection) -> None:
    """Create the backend-specific ``span_search`` (after the ``spans`` table exists)."""
    if conn.dialect.name == "sqlite":
        for stmt in SQLITE_SEARCH_DDL:
            conn.exec_driver_sql(stmt)
    else:
        pg_search_metadata.create_all(conn)
        conn.exec_driver_sql(PG_SEARCH_FK)


def create_all(conn: Connection) -> None:
    """Create every table of the current schema on a sync connection (tests and tooling)."""
    metadata.create_all(conn)
    create_search(conn)
