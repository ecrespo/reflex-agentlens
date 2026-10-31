"""Minimal rows in every table, shared by the schema and migration tests."""

from __future__ import annotations

from sqlalchemy import Connection, insert, text

from reflex_agentlens.store import schema as s

PROJECT_TABLES = (
    "api_keys",
    "redaction_rule_sets",
    "saved_filters",
    "sources",
    "resources",
    "sessions",
    "runs",
    "spans",
    "llm_calls",
    "tool_calls",
    "approvals",
    "graph_nodes",
    "metric_points",
)
NS = 1_700_000_000_000_000_000


def seed_project(conn: Connection, *, slug: str = "demo", n: int = 1) -> int:
    """Insert one Project with a row in every dependent table; return the project ``pk``."""
    project_pk = conn.execute(
        insert(s.projects)
        .values(id=f"01J{n:023d}", slug=slug, name=slug, created_at_ns=NS, updated_at_ns=NS)
        .returning(s.projects.c.pk)
    ).scalar_one()
    if conn.execute(text("SELECT count(*) FROM pricing_versions")).scalar_one() == 0:
        conn.execute(
            insert(s.pricing_versions).values(
                version=1, effective_from_ns=0, source="bundled", created_at_ns=NS
            )
        )
        conn.execute(
            insert(s.pricing_entries).values(
                version=1,
                model_pattern="gpt-4o",
                match_kind="exact",
                input_nusd_per_mtok=2_500_000_000,
                output_nusd_per_mtok=10_000_000_000,
            )
        )
    p = {"project_pk": project_pk}
    conn.execute(
        insert(s.api_keys).values(
            **p, key_id=f"abcd{n:04d}", salt=b"\0" * 16, secret_hash=b"\1" * 32, created_at_ns=NS
        )
    )
    conn.execute(insert(s.redaction_rule_sets).values(**p, updated_at_ns=NS))
    conn.execute(
        insert(s.saved_filters).values(
            **p,
            id=f"01K{n:023d}",
            name="f",
            query="status=error",
            created_at_ns=NS,
            updated_at_ns=NS,
        )
    )
    source_pk = conn.execute(
        insert(s.sources)
        .values(**p, service_name="svc", first_seen_ns=NS, last_seen_ns=NS)
        .returning(s.sources.c.pk)
    ).scalar_one()
    resource_pk = conn.execute(
        insert(s.resources)
        .values(**p, attrs_hash=b"\2" * 16, attributes={"service.name": "svc"})
        .returning(s.resources.c.pk)
    ).scalar_one()
    session_pk = conn.execute(
        insert(s.sessions)
        .values(
            **p,
            id=f"01M{n:023d}",
            external_id="conv-1",
            first_ns=NS,
            last_ns=NS,
            updated_at_ns=NS,
        )
        .returning(s.sessions.c.pk)
    ).scalar_one()
    run_pk = conn.execute(
        insert(s.runs)
        .values(
            **p,
            trace_id=bytes(range(16)),
            source_pk=source_pk,
            session_pk=session_pk,
            start_ns=NS,
            last_span_ns=NS,
            updated_at_ns=NS,
        )
        .returning(s.runs.c.pk)
    ).scalar_one()
    span_pk = conn.execute(
        insert(s.spans)
        .values(
            **p,
            run_pk=run_pk,
            span_id=bytes(range(8)),
            source_pk=source_pk,
            resource_pk=resource_pk,
            name="chat gpt-4o",
            start_ns=NS,
            end_ns=NS + 1,
            mapping_version=1,
            size_bytes=100,
            received_at_ns=NS,
        )
        .returning(s.spans.c.pk)
    ).scalar_one()
    d = {"span_pk": span_pk, "run_pk": run_pk, "project_pk": project_pk}
    conn.execute(
        insert(s.llm_calls).values(
            **d, start_ns=NS, duration_ns=1, operation="chat", unpriced=False, pricing_version=1
        )
    )
    conn.execute(insert(s.tool_calls).values(**d, start_ns=NS, duration_ns=1, status="ok"))
    conn.execute(
        insert(s.approvals).values(**d, kind="approval", decision="pending", requested_ns=NS)
    )
    conn.execute(
        insert(s.graph_nodes).values(
            **d,
            node_key="n",
            node_name="n",
            node_kind="node",
            start_ns=NS,
            end_ns=NS,
            status="ok",
        )
    )
    if conn.dialect.name == "sqlite":
        conn.execute(
            text("INSERT INTO span_search(rowid, doc) VALUES (:pk, 'hello world')"),
            {"pk": span_pk},
        )
    else:
        conn.execute(
            text(
                "INSERT INTO span_search(span_pk, run_pk, project_pk, tsv) "
                "VALUES (:s, :r, :p, to_tsvector('simple', 'hello world'))"
            ),
            {"s": span_pk, "r": run_pk, "p": project_pk},
        )
    conn.execute(
        insert(s.metric_points).values(
            **p,
            source_pk=source_pk,
            name="gen_ai.client.token.usage",
            kind="histogram",
            start_ns=NS,
            time_ns=NS,
            attrs_hash=b"\3" * 16,
            count=1,
            sum=1.0,
            bucket_counts=[1],
            explicit_bounds=[],
        )
    )
    return int(project_pk)


def search_hits(conn: Connection) -> int:
    if conn.dialect.name == "sqlite":
        sql = "SELECT count(*) FROM span_search WHERE span_search MATCH 'hello'"
    else:
        sql = "SELECT count(*) FROM span_search WHERE tsv @@ to_tsquery('simple', 'hello')"
    return int(conn.execute(text(sql)).scalar_one())


def counts(conn: Connection) -> dict[str, int]:
    out = {
        t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())  # noqa: S608
        for t in PROJECT_TABLES
    }
    out["span_search"] = search_hits(conn)
    return out
