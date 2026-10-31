"""Spike S2a: hot tables of Data Model §2.6–2.15 as SQLAlchemy 2.0 Core (Task P0-07.1).

One ``MetaData`` serves both backends; dialect differences from Data Model §2.0 are expressed with
``with_variant`` (types) and ``ddl_if`` (constraints and indexes that exist on one backend only).
``projects`` (§2.2) and ``pricing_versions`` (§2.5) are included because hot tables reference them.
``span_search`` differs structurally per backend (§2.15) and is created by ``create_search``.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
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

metadata = MetaData()

# --- §2.0 physical type mapping -------------------------------------------------------------

BIGINT = BigInteger().with_variant(Integer, "sqlite")  # SQLite: INTEGER (64-bit anyway)
JSONT = JSON().with_variant(JSONB(), "postgresql")


def pk() -> Column:
    """`PK`: SQLite INTEGER PRIMARY KEY (rowid alias); PG BIGINT GENERATED ALWAYS AS IDENTITY."""
    return Column("pk", BIGINT, Identity(always=True), primary_key=True)


def text_n(name: str, n: int, table: str, **kw) -> tuple[Column, CheckConstraint]:
    """`TEXT(n)`: PG VARCHAR(n); SQLite TEXT + CHECK length (SQLite does not enforce VARCHAR)."""
    col = Column(name, String(n), **kw)
    ck = CheckConstraint(f"length({name}) <= {n}", name=f"ck_{table}_{name}_len").ddl_if(
        dialect="sqlite"
    )
    return col, ck


def bytes_n(name: str, n: int, table: str, **kw) -> tuple[Column, CheckConstraint]:
    """`BYTES(n)`: BLOB/BYTEA + CHECK; ``length()`` counts bytes for BLOB and BYTEA alike."""
    col = Column(name, LargeBinary, **kw)
    return col, CheckConstraint(f"length({name}) = {n}", name=f"ck_{table}_{name}_len")


def boolean(name: str, **kw) -> Column:
    """`BOOL`: PG BOOLEAN; SQLite INTEGER with CHECK (col IN (0,1))."""
    return Column(name, Boolean(create_constraint=True, name=f"ck_bool_{name}"), **kw)


def json_col(name: str, table: str, default: str) -> tuple[Column, CheckConstraint]:
    """`JSON`: PG JSONB; SQLite TEXT + CHECK json_valid."""
    col = Column(name, JSONT, nullable=False, server_default=text(f"'{default}'"))
    ck = CheckConstraint(f"json_valid({name})", name=f"ck_{table}_{name}_json").ddl_if(
        dialect="sqlite"
    )
    return col, ck


def enum_check(name: str, values: tuple[str, ...], table: str) -> CheckConstraint:
    vals = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{name} IN ({vals})", name=f"ck_{table}_{name}")


def build(table: str, *items) -> Table:
    flat: list = []
    for it in items:
        flat.extend(it if isinstance(it, tuple) else (it,))
    return Table(table, metadata, *flat)


def fk(target: str, ondelete: str | None = "CASCADE") -> ForeignKey:
    return ForeignKey(target, ondelete=ondelete)


# --- referenced settings tables (§2.2, §2.5), minimal ------------------------------------------

projects = build(
    "projects",
    pk(),
    text_n("id", 26, "projects", nullable=False),
    text_n("slug", 40, "projects", nullable=False),
    text_n("name", 120, "projects", nullable=False),
    boolean("content_storage", nullable=False, server_default=text("true")),
    Column("retention_max_age_s", BIGINT, nullable=False, server_default=text("2592000")),
    Column("settings_version", Integer, nullable=False, server_default=text("1")),
    Column("created_at_ns", BIGINT, nullable=False),
    Column("updated_at_ns", BIGINT, nullable=False),
    CheckConstraint("retention_max_age_s >= 0", name="ck_projects_retention"),
)
Index("uq_projects_id", projects.c.id, unique=True)
Index("uq_projects_slug", projects.c.slug, unique=True)

pricing_versions = build(
    "pricing_versions",
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("effective_from_ns", BIGINT, nullable=False),
    text_n("source", 16, "pricing_versions", nullable=False),
    enum_check("source", ("bundled", "editor", "csv"), "pricing_versions"),
    Column("created_at_ns", BIGINT, nullable=False),
)

# --- §2.6 sources ----------------------------------------------------------------------------

sources = build(
    "sources",
    pk(),
    Column("project_pk", BIGINT, fk("projects.pk"), nullable=False),
    text_n("service_namespace", 128, "sources", nullable=False, server_default=text("''")),
    text_n("service_name", 256, "sources", nullable=False),
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

# --- §2.7 resources --------------------------------------------------------------------------

resources = build(
    "resources",
    pk(),
    Column("project_pk", BIGINT, fk("projects.pk"), nullable=False),
    bytes_n("attrs_hash", 16, "resources", nullable=False),
    json_col("attributes", "resources", "{}"),
    text_n("schema_url", 512, "resources", nullable=True),
)
Index("uq_resources", resources.c.project_pk, resources.c.attrs_hash, unique=True)

# --- §2.8 sessions ---------------------------------------------------------------------------

sessions = build(
    "sessions",
    pk(),
    text_n("id", 26, "sessions", nullable=False),
    Column("project_pk", BIGINT, fk("projects.pk"), nullable=False),
    text_n("external_id", 512, "sessions", nullable=False),
    Column("first_ns", BIGINT, nullable=False),
    Column("last_ns", BIGINT, nullable=False),
    Column("run_count", Integer, nullable=False, server_default=text("0")),
    Column("input_tokens", BIGINT, nullable=False, server_default=text("0")),
    Column("output_tokens", BIGINT, nullable=False, server_default=text("0")),
    Column("cached_input_tokens", BIGINT, nullable=False, server_default=text("0")),
    Column("cost_nusd", BIGINT, nullable=False, server_default=text("0")),
    boolean("includes_unpriced", nullable=False, server_default=text("false")),
    Column("updated_at_ns", BIGINT, nullable=False),
)
Index("uq_sessions_id", sessions.c.id, unique=True)
Index("uq_sessions_ext", sessions.c.project_pk, sessions.c.external_id, unique=True)
Index("ix_sessions_project_last", sessions.c.project_pk, sessions.c.last_ns.desc(), sessions.c.pk)

# --- §2.9 runs -------------------------------------------------------------------------------

runs = build(
    "runs",
    pk(),
    Column("project_pk", BIGINT, fk("projects.pk"), nullable=False),
    bytes_n("trace_id", 16, "runs", nullable=False),
    Column("source_pk", BIGINT, fk("sources.pk", None), nullable=True),
    Column("source_count", SmallInteger, nullable=False, server_default=text("1")),
    Column("session_pk", BIGINT, fk("sessions.pk", "SET NULL"), nullable=True),
    text_n("status", 12, "runs", nullable=False, server_default=text("'in_progress'")),
    enum_check("status", ("in_progress", "ok", "error", "incomplete"), "runs"),
    Column("start_ns", BIGINT, nullable=False),
    Column("end_ns", BIGINT, nullable=True),
    Column("duration_ns", BIGINT, nullable=True),
    Column("last_span_ns", BIGINT, nullable=False),
    Column("root_span_pk", BIGINT, nullable=True),
    text_n("root_name", 256, "runs", nullable=True),
    text_n("agent_name", 256, "runs", nullable=True),
    boolean("is_genai", nullable=False, server_default=text("false")),
    json_col("models", "runs", "[]"),
    *[
        Column(c, BIGINT, nullable=False, server_default=text("0"))
        for c in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        )
    ],
    text_n("usage_source", 8, "runs", nullable=False, server_default=text("'none'")),
    enum_check("usage_source", ("llm", "agent", "none"), "runs"),
    Column("cost_nusd", BIGINT, nullable=True),
    *[
        Column(c, Integer, nullable=False, server_default=text("0"))
        for c in (
            "unpriced_calls",
            "llm_calls",
            "tool_calls",
            "error_spans",
            "span_count",
            "missing_parents",
        )
    ],
    Column("bytes_est", BIGINT, nullable=False, server_default=text("0")),
    Column("updated_at_ns", BIGINT, nullable=False),
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

# --- §2.10 spans -----------------------------------------------------------------------------

spans = build(
    "spans",
    pk(),
    Column("project_pk", BIGINT, fk("projects.pk"), nullable=False),
    Column("run_pk", BIGINT, fk("runs.pk"), nullable=False),
    bytes_n("span_id", 8, "spans", nullable=False),
    Column("parent_span_id", LargeBinary, nullable=True),
    CheckConstraint(
        "parent_span_id IS NULL OR length(parent_span_id) = 8", name="ck_spans_parent_len"
    ),
    Column("source_pk", BIGINT, fk("sources.pk", None), nullable=False),
    Column("resource_pk", BIGINT, fk("resources.pk", None), nullable=False),
    text_n("name", 1024, "spans", nullable=False),
    Column("otel_kind", SmallInteger, nullable=False, server_default=text("0")),
    text_n("category", 14, "spans", nullable=False, server_default=text("'generic'")),
    enum_check(
        "category", ("llm", "tool", "agent", "approval", "generic_genai", "generic"), "spans"
    ),
    boolean("is_genai", nullable=False, server_default=text("false")),
    Column("start_ns", BIGINT, nullable=False),
    Column("end_ns", BIGINT, nullable=False),
    CheckConstraint("end_ns >= start_ns", name="ck_spans_time"),
    Column("status_code", SmallInteger, nullable=False, server_default=text("0")),
    Column("status_message", Text, nullable=True),
    text_n("scope_name", 256, "spans", nullable=True),
    text_n("scope_version", 64, "spans", nullable=True),
    json_col("attributes", "spans", "{}"),
    json_col("events", "spans", "[]"),
    json_col("links", "spans", "[]"),
    json_col("dropped_counts", "spans", "{}"),
    json_col("dropped_keys", "spans", "[]"),
    text_n("agent_name", 256, "spans", nullable=True),
    text_n("session_ext", 512, "spans", nullable=True),
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


def derived(table: str, *items) -> Table:
    """Derived per-span tables share `span_pk` PK/FK, `run_pk` FK and denormalized `project_pk`."""
    return build(
        table,
        Column("span_pk", BIGINT, fk("spans.pk"), primary_key=True, autoincrement=False),
        Column("run_pk", BIGINT, fk("runs.pk"), nullable=False),
        Column("project_pk", BIGINT, nullable=False),
        *items,
    )


# --- §2.11 llm_calls -------------------------------------------------------------------------

LLM_COVER = ("input_tokens", "output_tokens", "cached_input_tokens", "cost_nusd", "duration_ns")

llm_calls = derived(
    "llm_calls",
    Column("start_ns", BIGINT, nullable=False),
    Column("duration_ns", BIGINT, nullable=False),
    text_n("operation", 32, "llm_calls", nullable=False),
    text_n("provider", 64, "llm_calls", nullable=True),
    text_n("request_model", 200, "llm_calls", nullable=True),
    text_n("response_model", 200, "llm_calls", nullable=True),
    text_n("model", 200, "llm_calls", nullable=False, server_default=text("'unknown'")),
    *[
        Column(c, BIGINT, nullable=True)
        for c in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "cost_nusd",
        )
    ],
    boolean("unpriced", nullable=False),
    Column("pricing_version", Integer, fk("pricing_versions.version", None), nullable=True),
    text_n("status", 5, "llm_calls", nullable=False, server_default=text("'ok'")),
    enum_check("status", ("ok", "error"), "llm_calls"),
    text_n("error_type", 128, "llm_calls", nullable=True),
    json_col("finish_reasons", "llm_calls", "[]"),
    json_col("params", "llm_calls", "{}"),
    boolean("content_dropped", nullable=False, server_default=text("false")),
)
Index(
    "ix_llm_project_start",
    llm_calls.c.project_pk,
    llm_calls.c.start_ns,
    llm_calls.c.model,
    postgresql_include=[*LLM_COVER, "operation"],
).ddl_if(dialect="postgresql")
Index(  # SQLite has no INCLUDE: covering columns appended to the key (§2.11)
    "ix_llm_project_start",
    llm_calls.c.project_pk,
    llm_calls.c.start_ns,
    llm_calls.c.model,
    *[llm_calls.c[c] for c in (*LLM_COVER, "operation")],
).ddl_if(dialect="sqlite")
Index("ix_llm_project_model_run", llm_calls.c.project_pk, llm_calls.c.model, llm_calls.c.run_pk)
Index("ix_llm_run", llm_calls.c.run_pk)

# --- §2.12 tool_calls ------------------------------------------------------------------------

tool_calls = derived(
    "tool_calls",
    Column("start_ns", BIGINT, nullable=False),
    Column("duration_ns", BIGINT, nullable=False),
    text_n("tool_name", 256, "tool_calls", nullable=False, server_default=text("'unknown'")),
    text_n("tool_call_id", 256, "tool_calls", nullable=True),
    text_n("tool_type", 32, "tool_calls", nullable=True),
    text_n("status", 5, "tool_calls", nullable=False),
    enum_check("status", ("ok", "error"), "tool_calls"),
    text_n("error_type", 128, "tool_calls", nullable=True),
    boolean("content_dropped", nullable=False, server_default=text("false")),
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

# --- §2.13 approvals -------------------------------------------------------------------------

approvals = derived(
    "approvals",
    text_n("approval_key", 256, "approvals", nullable=True),
    text_n("kind", 12, "approvals", nullable=False),
    enum_check("kind", ("approval", "deferral", "elicitation"), "approvals"),
    text_n("decision", 10, "approvals", nullable=False),
    enum_check("decision", ("approved", "rejected", "edited", "timeout", "pending"), "approvals"),
    text_n("approver", 256, "approvals", nullable=True),
    Column("requested_ns", BIGINT, nullable=False),
    Column("decided_ns", BIGINT, nullable=True),
    Column("wait_ns", BIGINT, nullable=True),
    text_n("tool_call_id", 256, "approvals", nullable=True),
    text_n("tool_name", 256, "approvals", nullable=True),
    text_n("node_name", 256, "approvals", nullable=True),
)
Index(
    "ix_appr_project_requested",
    approvals.c.project_pk,
    approvals.c.requested_ns,
    approvals.c.decision,
    approvals.c.wait_ns,
)
Index("ix_appr_run", approvals.c.run_pk)

# --- §2.14 graph_nodes -----------------------------------------------------------------------

graph_nodes = derived(
    "graph_nodes",
    text_n("node_key", 256, "graph_nodes", nullable=False),
    text_n("node_name", 256, "graph_nodes", nullable=False),
    text_n("node_kind", 8, "graph_nodes", nullable=False),
    enum_check("node_kind", ("agent", "workflow", "node"), "graph_nodes"),
    text_n("explicit_parent_key", 256, "graph_nodes", nullable=True),
    Column("step", Integer, nullable=True),
    text_n("agent_name", 256, "graph_nodes", nullable=True),
    Column("start_ns", BIGINT, nullable=False),
    Column("end_ns", BIGINT, nullable=False),
    text_n("status", 5, "graph_nodes", nullable=False),
    enum_check("status", ("ok", "error"), "graph_nodes"),
)
Index(
    "ix_gn_project_start",
    graph_nodes.c.project_pk,
    graph_nodes.c.start_ns,
    graph_nodes.c.node_name,
    graph_nodes.c.end_ns,
)
Index("ix_gn_run", graph_nodes.c.run_pk)

# --- §2.15 span_search -----------------------------------------------------------------------

pg_search_metadata = MetaData()
span_search_pg = Table(
    "span_search",
    pg_search_metadata,
    Column("span_pk", BigInteger, ForeignKey(spans.c.pk, ondelete="CASCADE"), primary_key=True),
    Column("run_pk", BigInteger, nullable=False),
    Column("project_pk", BigInteger, nullable=False),
    Column("tsv", TSVECTOR, nullable=False),
)
Index("ix_search_tsv", span_search_pg.c.tsv, postgresql_using="gin")
Index("ix_search_run", span_search_pg.c.run_pk)

SQLITE_SEARCH_DDL = (
    "CREATE VIRTUAL TABLE span_search USING fts5(doc, content='', contentless_delete=1, "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER trg_spans_search_delete AFTER DELETE ON spans "
    "BEGIN DELETE FROM span_search WHERE rowid = old.pk; END",
)


def create_search(sync_conn) -> None:
    """Create the backend-specific `span_search` (call inside ``run_sync`` after create_all)."""
    if sync_conn.dialect.name == "sqlite":
        for stmt in SQLITE_SEARCH_DDL:
            sync_conn.exec_driver_sql(stmt)
    else:
        pg_search_metadata.create_all(sync_conn)
