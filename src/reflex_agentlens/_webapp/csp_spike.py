"""Spike S1d: CSP with build-time script hashes (Task P0-05.1, DD-018, Amendment A5).

``inline_script_hashes`` extracts every inline ``<script>`` of an exported HTML page and returns
CSP source expressions (``'sha256-…'``). ``csp_middleware`` adds the policy from the
``AGENTLENS_SPIKE_CSP`` environment variable (set by boot.py before Granian forks) to every HTTP
response of the Reflex backend, static frontend included.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def inline_script_hashes(html: str) -> list[str]:
    out = []
    for body in INLINE_SCRIPT.findall(html):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        out.append(f"'sha256-{digest}'")
    return out


def policy(hashes: list[str], ws_origin: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' {' '.join(sorted(set(hashes)))}; "
        "style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {ws_origin}; "
        "img-src 'self' data:; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )


def csp_middleware(app):
    header = os.environ.get("AGENTLENS_SPIKE_CSP")
    if not header:
        return app
    raw = (b"content-security-policy", header.encode())

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def send_with_csp(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), raw]}
            await send(message)

        return await app(scope, receive, send_with_csp)

    return wrapped
