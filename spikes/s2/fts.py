# /// script
# requires-python = ">=3.11"
# dependencies = ["asyncpg>=0.32.0"]
# ///
"""Spike S2c: full-text parity on identifiers, FTS5 vs. PostgreSQL 'simple' (P0-09.3, A-15).

Indexes documents holding identifiers on both backends, runs every term through the DD-006 query
builder (lowercase, ``[\\p{L}\\p{N}_]+`` tokens, AND; FTS5 ``"t1" "t2"``, PG
``to_tsquery('simple', 't1 & t2')``) and through a candidate builder, and prints term -> matching
document IDs per backend plus how each backend tokenizes the documents.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import unicodedata

import asyncpg

DOCS = {
    1: "execute_tool search_web best pizza near me",
    2: "execute_tool db.query select * from orders",
    3: "chat gpt-4o-mini summarize the invoice",
    4: "execute_tool RefundTool refund order 991",
    5: "web search for café opening hours",
    6: "query the db for orders",
    7: "chat gpt-4o hello",
    8: "kb-lookup policy warranty; sql.execute failed",
    9: "Résumé naïve CAFÉ",
    10: "search web results and refundtool docs",
}
TERMS = [
    "search_web",
    "search",
    "web",
    "db.query",
    "db",
    "query",
    "gpt-4o-mini",
    "gpt-4o",
    "4o",
    "mini",
    "RefundTool",
    "refundtool",
    "kb-lookup",
    "sql.execute",
    "cafe",
    "café",
    "resume",
    "search web",
]
PG = "postgresql://postgres:spike@127.0.0.1:55432/agentlens"


def dd006_tokens(term: str) -> list[str]:
    return re.findall(r"[^\W]+", term.lower())  # [\p{L}\p{N}_]+ (Python \w includes "_")


def normalize(text: str) -> str:
    """Candidate DD-006 rule, applied to documents AND queries in Python before either backend:
    NFKD + drop combining marks, lowercase, every non-letter/digit (incl. "_", ".", "-") -> space."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", folded.lower()).strip()


def candidate_tokens(term: str) -> list[str]:
    return normalize(term).split()


def fts5_query(tokens: list[str]) -> str:
    return " ".join(f'"{t}"' for t in tokens)


def pg_query(tokens: list[str]) -> str:
    return " & ".join(tokens)


async def main() -> None:
    lite = sqlite3.connect(":memory:")
    lite.execute(
        "CREATE VIRTUAL TABLE f USING fts5(doc, content='', contentless_delete=1, "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    lite.execute("CREATE VIRTUAL TABLE v USING fts5vocab(f, 'instance')")
    lite.executemany("INSERT INTO f(rowid, doc) VALUES (?, ?)", list(DOCS.items()))
    pg = await asyncpg.connect(PG)
    await pg.execute("CREATE TEMP TABLE f (id int, tsv tsvector)")
    await pg.executemany("INSERT INTO f VALUES ($1, to_tsvector('simple', $2))", list(DOCS.items()))
    lite.execute(
        "CREATE VIRTUAL TABLE f2 USING fts5(doc, content='', contentless_delete=1, "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    lite.executemany(
        "INSERT INTO f2(rowid, doc) VALUES (?, ?)", [(i, normalize(d)) for i, d in DOCS.items()]
    )
    await pg.execute("CREATE TEMP TABLE f2 (id int, tsv tsvector)")
    await pg.executemany(
        "INSERT INTO f2 VALUES ($1, to_tsvector('simple', $2))",
        [(i, normalize(d)) for i, d in DOCS.items()],
    )
    tokenization = {}
    for i, d in DOCS.items():
        lt = [r[0] for r in lite.execute("SELECT term FROM v WHERE doc = ? ORDER BY offset", (i,))]
        pt = await pg.fetchval("SELECT to_tsvector('simple', $1)::text", d)
        tokenization[d] = {"fts5": lt, "pg": pt}
    results = {}
    for builder, tok, tbl in (
        ("dd006", dd006_tokens, "f"),
        ("normalized_docs_and_queries", candidate_tokens, "f2"),
    ):
        rows = {}
        for term in TERMS:
            tokens = tok(term)
            lite_ids = sorted(
                r[0]
                for r in lite.execute(
                    f"SELECT rowid FROM {tbl} WHERE {tbl} MATCH ?", (fts5_query(tokens),)
                )
            )
            pg_ids = sorted(
                r["id"]
                for r in await pg.fetch(
                    f"SELECT id FROM {tbl} WHERE tsv @@ to_tsquery('simple', $1)", pg_query(tokens)
                )
            )
            pg_tsq = await pg.fetchval("SELECT to_tsquery('simple', $1)::text", pg_query(tokens))
            rows[term] = {
                "tokens": tokens,
                "fts5": lite_ids,
                "pg": pg_ids,
                "pg_tsquery": pg_tsq,
                "same": lite_ids == pg_ids,
            }
        results[builder] = {"all_same": all(r["same"] for r in rows.values()), "terms": rows}
    await pg.close()
    print(json.dumps({"tokenization": tokenization, **results}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
