"""Safe rich-text policy (plan task P6-06.1; REQ-193, 327).

Captured content (LLM messages, tool output) is hostile input. The Markdown renderer
(``components/richtext.py``) runs with raw HTML disabled and this URL policy: only ``http``,
``https`` and ``mailto`` survive; everything else (``javascript:``, ``data:``, ``vbscript:``,
relative and protocol-relative URLs) becomes an empty URL. Images are never fetched: they are
rendered as links to their source. :data:`URL_TRANSFORM_JS` is the same policy for the browser;
both are tested against one fixture list.
"""

from __future__ import annotations

import re

__all__ = ["SAFE_SCHEMES", "URL_TRANSFORM_JS", "safe_url"]

SAFE_SCHEMES = ("http", "https", "mailto")
_SAFE = re.compile(r"^(?:https?|mailto):", re.IGNORECASE)
# control characters and whitespace a browser strips before reading the scheme
_STRIP = re.compile(r"[\x00-\x20\x7f]+")

# react-markdown ``urlTransform(url, key, node)``: the browser half of :func:`safe_url`
URL_TRANSFORM_JS = (
    "(url) => { const u = String(url == null ? '' : url).replace(/[\\u0000-\\u0020\\u007f]+/g, '');"
    " return /^(?:https?|mailto):/i.test(u) ? u : ''; }"
)


def safe_url(url: str | None) -> str:
    """``url`` without control characters when its scheme is allowed, else ``""``."""
    cleaned = _STRIP.sub("", url or "")
    return cleaned if _SAFE.match(cleaned) else ""
