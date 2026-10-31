"""Retention purge and storage usage (plan task P2-09.1; Data Model Q13, §2.18, §6).

REQ-090 (by age), REQ-091 (by size, down to <= 90 % of the limit), REQ-092.b/c (one job at a
time, bounded batches), REQ-173 (usage and last purge).

Each function deletes **one batch** of whole traces in the caller's transaction (the backend
loops, one transaction per batch, on the writer). A batch locks its Runs in (``project_pk``,
``trace_id``) order (DD-004), deletes them (spans, derived rows and search rows cascade),
recomputes the Sessions they belonged to and deletes Sessions left without Runs. Projects, API
keys, pricing, redaction rules and saved filters are never touched (REQ-091.b).

Resource rows no span references any more are removed by :func:`cleanup` at the end of a purge,
in a separate transaction; on PostgreSQL a writer may reuse such a row concurrently, in which
case the ``RESTRICT`` foreign key makes the cleanup fail and it is simply skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import BigInteger, Connection, case, delete, func, insert, literal, select, text

from reflex_agentlens.store import schema as s
from reflex_agentlens.store.recompute import recompute_sessions
from reflex_agentlens.store.records import PurgeReport, StorageUsage

__all__ = [
    "METRIC_POINT_BYTES_EST",
    "PURGE_LOG_KEEP",
    "age_batch",
    "cleanup",
    "estimated_bytes",
    "last_purge",
    "record_purge",
    "size_batch",
    "storage_usage",
]

METRIC_POINT_BYTES_EST = 200  # rough per-row estimate; metric points are small and fixed-shape
PURGE_LOG_KEEP = 500
NS_PER_S = 1_000_000_000
r, p, ses = s.runs, s.projects, s.sessions


def _max_age(now_ns: int) -> int:
    """Ages beyond this put the cutoff before the epoch; clamping avoids BIGINT overflow."""
    return max(now_ns // NS_PER_S, 0)


def _cutoff(now_ns: int, max_age_s: Any) -> Any:
    """``now - max_age`` in ns, never overflowing for any stored age (SQL or Python value)."""
    cap = _max_age(now_ns)
    if isinstance(max_age_s, int):
        return now_ns - min(max_age_s, cap) * NS_PER_S
    big = literal(cap, BigInteger)
    clamped = case((max_age_s > big, big), else_=max_age_s)
    return literal(now_ns, BigInteger) - clamped * literal(NS_PER_S, BigInteger)


def _delete_runs(conn: Connection, run_pks: Sequence[int], now_ns: int) -> int:
    """Delete whole traces; returns their estimated bytes."""
    stmt = select(r.c.pk, r.c.bytes_est, r.c.session_pk).where(r.c.pk.in_(run_pks))
    stmt = stmt.order_by(r.c.project_pk, r.c.trace_id)
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update(of=r)
    rows = conn.execute(stmt).all()
    if not rows:
        return 0
    pks = [row[0] for row in rows]
    sessions = sorted({row[2] for row in rows if row[2] is not None})
    conn.execute(delete(r).where(r.c.pk.in_(pks)))
    if sessions:
        recompute_sessions(conn, sessions, now_ns)
        conn.execute(delete(ses).where(ses.c.pk.in_(sessions), ses.c.run_count == 0))
    return int(sum(row[1] for row in rows))


def age_batch(conn: Connection, now_ns: int, batch_traces: int) -> tuple[int, int]:
    """Q13 by age: the oldest ``batch_traces`` traces past their Project's maximum age."""
    cutoff = _cutoff(now_ns, p.c.retention_max_age_s)
    pks = list(
        conn.execute(
            select(r.c.pk)
            .select_from(r.join(p, p.c.pk == r.c.project_pk))
            .where(p.c.retention_max_age_s > 0, r.c.last_span_ns < cutoff)
            .order_by(r.c.last_span_ns, r.c.pk)
            .limit(batch_traces)
        ).scalars()
    )
    return len(pks), _delete_runs(conn, pks, now_ns) if pks else 0


def size_batch(
    conn: Connection, bytes_to_free: int, batch_traces: int, now_ns: int
) -> tuple[int, int]:
    """Q13 by size: the globally oldest traces, at most ``batch_traces``, until enough is freed."""
    rows = conn.execute(
        select(r.c.pk, r.c.bytes_est).order_by(r.c.last_span_ns, r.c.pk).limit(batch_traces)
    ).all()
    pks, freed = [], 0
    for pk, size in rows:
        if freed >= bytes_to_free:
            break
        pks.append(pk)
        freed += size
    return len(pks), _delete_runs(conn, pks, now_ns) if pks else 0


def estimated_bytes(conn: Connection) -> int:
    """REQ-091 usage: span bytes of every Run plus a per-row estimate for metric points."""
    runs = conn.execute(select(func.coalesce(func.sum(r.c.bytes_est), 0))).scalar_one()
    points = conn.execute(select(func.count()).select_from(s.metric_points)).scalar_one()
    return int(runs) + int(points) * METRIC_POINT_BYTES_EST


def cleanup(conn: Connection, now_ns: int) -> None:
    """Expired metric points, unreferenced resources; SQLite returns free pages to the OS."""
    mp = s.metric_points
    expired = select(p.c.pk, p.c.retention_max_age_s).where(p.c.retention_max_age_s > 0)
    for project_pk, max_age in conn.execute(expired).all():
        conn.execute(
            delete(mp).where(mp.c.project_pk == project_pk,
                             mp.c.time_ns < _cutoff(now_ns, int(max_age)))
        )  # fmt: skip
    res = s.resources
    conn.execute(delete(res).where(res.c.pk.not_in(select(s.spans.c.resource_pk).distinct())))
    if conn.dialect.name == "sqlite":
        conn.execute(text("PRAGMA incremental_vacuum(4096)"))


def storage_usage(conn: Connection) -> StorageUsage:
    """REQ-173.a: estimated bytes (what REQ-091 compares) and physical bytes on disk."""
    runs, spans = conn.execute(
        select(func.count(), func.coalesce(func.sum(r.c.span_count), 0)).select_from(r)
    ).one()
    if conn.dialect.name == "sqlite":
        pages = conn.execute(text("PRAGMA page_count")).scalar_one()
        size = conn.execute(text("PRAGMA page_size")).scalar_one()
        physical: int | None = int(pages) * int(size)
    else:
        physical = conn.execute(
            text(
                "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'm')"
            )
        ).scalar_one()
        physical = int(physical)
    return StorageUsage(
        estimated_bytes=estimated_bytes(conn),
        physical_bytes=physical,
        runs=int(runs),
        spans=int(spans),
    )


def _report(m: Any) -> PurgeReport:
    return PurgeReport(
        trigger=m["trigger"],
        started_ns=m["started_ns"],
        finished_ns=m["finished_ns"],
        deleted_traces_age=m["deleted_traces_age"],
        deleted_traces_size=m["deleted_traces_size"],
        deleted_bytes_est=m["deleted_bytes_est"],
        error_class=m["error_class"],
    )


def record_purge(conn: Connection, report: PurgeReport) -> None:
    """Append to ``purge_log`` and keep the newest ``PURGE_LOG_KEEP`` rows (§2.18)."""
    log = s.purge_log
    conn.execute(
        insert(log).values(
            trigger=report.trigger,
            started_ns=report.started_ns,
            finished_ns=report.finished_ns,
            deleted_traces_age=report.deleted_traces_age,
            deleted_traces_size=report.deleted_traces_size,
            deleted_bytes_est=report.deleted_bytes_est,
            error_class=report.error_class,
        )
    )
    keep = select(log.c.pk).order_by(log.c.pk.desc()).limit(PURGE_LOG_KEEP).subquery()
    conn.execute(delete(log).where(log.c.pk.not_in(select(keep.c.pk))))


def last_purge(conn: Connection) -> PurgeReport | None:
    log = s.purge_log
    row = conn.execute(select(log).order_by(log.c.pk.desc()).limit(1)).mappings().first()
    return None if row is None else _report(row)
