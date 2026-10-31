"""Search text normalization shared by ingest and store (Tech Design DD-006, Analyze A-15).

Documents are stored already normalized, and queries go through the same function, so FTS5
(``unicode61 remove_diacritics 2``) and PostgreSQL (``to_tsvector('simple', …)``) see identical
tokens: NFKD, combining marks dropped, lowercase, and every run of characters that is not a
letter or digit (including ``_``, ``.``, ``-``) replaced by one space.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["MAX_SEARCH_DOC_BYTES", "search_normalize", "search_tokens", "truncate_utf8"]

MAX_SEARCH_DOC_BYTES = 32 * 1024
_NON_WORD = re.compile(r"[\W_]+")


def search_normalize(text: str) -> str:
    """Normalize a document or a query per DD-006."""
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_WORD.sub(" ", folded.lower()).strip()


def search_tokens(text: str) -> list[str]:
    """Query tokens (combined with AND by the backends); empty for a query with no terms."""
    return search_normalize(text).split()


def truncate_utf8(text: str, max_bytes: int = MAX_SEARCH_DOC_BYTES) -> str:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes without splitting a character."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")
