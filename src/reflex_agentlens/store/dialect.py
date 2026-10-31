"""Dialect operations shared by the SQL backends (plan task P2-03.1; DD-005, DD-006, REQ-325.b).

Every statement here is built from **trusted fragments** (column names and sub-selects written in
this package) plus **bound parameters** for anything that comes from a user or a payload: FTS
queries, attribute keys and id lists. Percentiles are validated floats rendered as literals,
because PostgreSQL's ``percentile_disc`` needs them per call and both backends must compute the
rank from the same IEEE double.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, Table, bindparam, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.sql import Executable

from reflex_agentlens.core.search import search_tokens

__all__ = [
    "FtsQuery",
    "PostgresOps",
    "SqlDialectOps",
    "SqliteOps",
    "ops_for",
    "percentile_rank",
]


def percentile_rank(p: float, n: int) -> int:
    """Nearest rank ⌈p·n⌉ with the product in IEEE double, exactly as PostgreSQL computes it."""
    return max(1, math.ceil(p * n))


def _check_percentiles(ps: Sequence[float]) -> list[str]:
    if not ps:
        raise ValueError("at least one percentile is required")
    out = []
    for p in ps:
        if type(p) is not float or not 0.0 < p <= 1.0:
            raise ValueError(f"percentile must be a float in (0, 1], got {p!r}")
        out.append(repr(p))
    return out


@dataclass(frozen=True, slots=True)
class FtsQuery:
    """A full-text predicate: constant SQL text plus the bound query string."""

    sql: str
    params: dict[str, str]


class SqlDialectOps:
    """Operations whose SQL differs between SQLite and PostgreSQL."""

    name: ClassVar[str]
    fts_param: ClassVar[str] = "fts_q"

    # ── inserts ──────────────────────────────────────────────────────────────────────────────

    def insert(self, table: Table) -> Any:
        """A dialect ``INSERT`` with ``on_conflict_do_nothing`` / ``on_conflict_do_update``."""
        raise NotImplementedError

    def greatest(self, a: Any, b: Any) -> ColumnElement[Any]:
        raise NotImplementedError

    def least(self, a: Any, b: Any) -> ColumnElement[Any]:
        raise NotImplementedError

    # ── id sets ──────────────────────────────────────────────────────────────────────────────

    def id_set_sql(self, param: str) -> str:
        """``SELECT … AS v`` yielding one row per id of the bound list ``:param``."""
        raise NotImplementedError

    def id_set_value(self, ids: Sequence[int]) -> Any:
        raise NotImplementedError

    # ── JSON ─────────────────────────────────────────────────────────────────────────────────

    def json_get(self, column: ColumnElement[Any], key: str) -> ColumnElement[Any]:
        """Top-level member ``key`` of a JSON ``column`` (string members come back as text).

        The key is always a bound parameter, never part of the SQL text (REQ-325.b).
        """
        raise NotImplementedError

    # ── full-text search (DD-006, REQ-325.b) ─────────────────────────────────────────────────

    def fts_query(self, user_text: str) -> FtsQuery | None:
        """The ``span_search`` predicate for a user query; ``None`` when it has no terms."""
        raise NotImplementedError

    def search_insert(self) -> Executable:
        """Insert one search document (binds ``pk``, ``run_pk``, ``project_pk``, ``doc``)."""
        raise NotImplementedError

    # ── percentiles (DD-005, Data Model Q8) ──────────────────────────────────────────────────

    def percentile_sql(
        self, ps: Sequence[float], value: str, group: Sequence[str], sources: Sequence[str]
    ) -> str:
        """Nearest-rank percentiles ``p0, p1, …`` of ``value`` per ``group`` in one sort.

        ``sources`` are trusted sub-selects producing the ``group`` columns and ``value``; a group
        never spans two sources (SQLite numbers each source separately).
        """
        raise NotImplementedError


class SqliteOps(SqlDialectOps):
    name = "sqlite"

    def insert(self, table: Table) -> Any:
        return sqlite.insert(table)

    def greatest(self, a: Any, b: Any) -> ColumnElement[Any]:
        return func.max(a, b)

    def least(self, a: Any, b: Any) -> ColumnElement[Any]:
        return func.min(a, b)

    def id_set_sql(self, param: str) -> str:
        return f"SELECT value AS v FROM json_each(:{param})"  # noqa: S608 - bind name only

    def id_set_value(self, ids: Sequence[int]) -> Any:
        return json.dumps([int(i) for i in ids])

    def json_get(self, column: ColumnElement[Any], key: str) -> ColumnElement[Any]:
        if '"' in key or "\\" in key:  # not expressible as a quoted JSON path label
            each = func.json_each(column).table_valued("key", "value").alias("je")
            return select(each.c.value).where(each.c.key == bindparam(None, key)).scalar_subquery()
        return func.json_extract(column, bindparam(None, f'$."{key}"'))

    def fts_query(self, user_text: str) -> FtsQuery | None:
        tokens = search_tokens(user_text)
        if not tokens:
            return None
        # Each token becomes an FTS5 string ("…"), so no token can act as an operator.
        match = " ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        return FtsQuery(f"span_search MATCH :{self.fts_param}", {self.fts_param: match})

    def search_insert(self) -> Executable:
        return text("INSERT INTO span_search (rowid, doc) VALUES (:pk, :doc)")

    def percentile_sql(
        self, ps: Sequence[float], value: str, group: Sequence[str], sources: Sequence[str]
    ) -> str:
        lits = _check_percentiles(ps)
        g = ", ".join(group)
        part = f"PARTITION BY {g}" if group else ""

        def rank(p: str) -> str:
            x = f"({p} * cnt)"
            return f"CAST({x} AS INTEGER) + ({x} > CAST({x} AS INTEGER))"

        picks = ", ".join(
            f"max(CASE WHEN rn = {rank(p)} THEN {value} END) AS p{i}" for i, p in enumerate(lits)
        )
        head = f"{g}, " if group else ""
        tail = f" GROUP BY {g}" if group else ""
        return " UNION ALL ".join(
            f"SELECT {head}{picks} FROM (SELECT {head}{value}, "  # noqa: S608 - trusted fragments
            f"ROW_NUMBER() OVER ({part} ORDER BY {value}) AS rn, "
            f"count(*) OVER ({part}) AS cnt FROM ({src}) AS s) AS w{tail}"
            for src in sources
        )


class PostgresOps(SqlDialectOps):
    name = "postgresql"

    def insert(self, table: Table) -> Any:
        return postgresql.insert(table)

    def greatest(self, a: Any, b: Any) -> ColumnElement[Any]:
        return func.greatest(a, b)

    def least(self, a: Any, b: Any) -> ColumnElement[Any]:
        return func.least(a, b)

    def id_set_sql(self, param: str) -> str:
        return f"SELECT unnest(CAST(:{param} AS BIGINT[])) AS v"

    def id_set_value(self, ids: Sequence[int]) -> Any:
        return [int(i) for i in ids]

    def json_get(self, column: ColumnElement[Any], key: str) -> ColumnElement[Any]:
        return column.op("->>")(bindparam(None, key))

    def fts_query(self, user_text: str) -> FtsQuery | None:
        tokens = search_tokens(user_text)
        if not tokens:
            return None
        # Quoted lexemes ('…') joined by AND: no token can act as a tsquery operator.
        query = " & ".join("'" + t.replace("'", "''") + "'" for t in tokens)
        return FtsQuery(
            f"span_search.tsv @@ to_tsquery('simple', :{self.fts_param})",
            {self.fts_param: query},
        )

    def search_insert(self) -> Executable:
        return text(
            "INSERT INTO span_search (span_pk, run_pk, project_pk, tsv) "
            "VALUES (:pk, :run_pk, :project_pk, to_tsvector('simple', :doc))"
        )

    def percentile_sql(
        self, ps: Sequence[float], value: str, group: Sequence[str], sources: Sequence[str]
    ) -> str:
        lits = _check_percentiles(ps)
        g = ", ".join(group)
        head = f"{g}, " if group else ""
        tail = f" GROUP BY {g}" if group else ""
        picks = ", ".join(f"v[{i + 1}] AS p{i}" for i in range(len(lits)))
        union = " UNION ALL ".join(sources)
        return (
            f"SELECT {head}{picks} FROM (SELECT {head}"  # noqa: S608 - trusted fragments
            f"percentile_disc(ARRAY[{', '.join(lits)}]) "
            f"WITHIN GROUP (ORDER BY {value}) AS v FROM ({union}) AS s{tail}) AS z"
        )


_OPS: dict[str, SqlDialectOps] = {"sqlite": SqliteOps(), "postgresql": PostgresOps()}


def ops_for(dialect_name: str) -> SqlDialectOps:
    try:
        return _OPS[dialect_name]
    except KeyError:
        raise ValueError(f"unsupported dialect {dialect_name!r}") from None
