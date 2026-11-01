"""Quickstart text for the empty state (plan task P6-01.2; REQ-130.b).

The same environment variables as the key-creation snippet (API §6.4), with a placeholder in
place of a key: the empty state never shows a real key.
"""

from __future__ import annotations

from reflex_agentlens.core.config import AgentlensConfig

__all__ = ["KEY_PLACEHOLDER", "otlp_base_url", "quickstart_snippet"]

KEY_PLACEHOLDER = "<your API key>"


def otlp_base_url(cfg: AgentlensConfig | None) -> str:
    if cfg is None:
        return "http://127.0.0.1:8000"
    server = cfg.server
    if server.public_url is not None:
        return str(server.public_url).rstrip("/")
    host = server.http_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{server.http_port}"


def quickstart_snippet(cfg: AgentlensConfig | None) -> str:
    return (
        f'export OTEL_EXPORTER_OTLP_ENDPOINT="{otlp_base_url(cfg)}"\n'
        'export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"\n'
        f'export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20{KEY_PLACEHOLDER}"\n'
    )
