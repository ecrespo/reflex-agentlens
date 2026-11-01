"""First-run bootstrap and the session secret (plan task P5-02.2; REQ-201, 321.a, 323.a).

On an empty database ``serve`` creates Project ``default`` and one API key. The plaintext never
goes through ``logging`` (REQ-323): on a TTY it is written straight into the banner, otherwise to
``{data_dir}/bootstrap-api-key`` (0600) and the banner names that file. A key configured through
``bootstrap.api_key`` is stored hashed under its own ``key_id`` and never printed (REQ-201.b).

The session secret signs cookies and cursors: ``auth.session_secret`` when set, else 32 random
bytes kept hex-encoded in ``{data_dir}/session_secret`` (0600) and reused across starts.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from reflex_agentlens import __version__
from reflex_agentlens.core.config import AgentlensConfig
from reflex_agentlens.core.errors import ConfigError, Conflict
from reflex_agentlens.core.security import hash_secret, parse_api_key
from reflex_agentlens.query import dto
from reflex_agentlens.query.admin import AdminService
from reflex_agentlens.store.protocol import StorageBackend
from reflex_agentlens.store.records import NewApiKeyRecord

__all__ = [
    "KEY_FILE",
    "SECRET_FILE",
    "SYSTEM",
    "BootstrapResult",
    "announce",
    "banner",
    "first_run",
    "session_secret",
]

KEY_FILE = "bootstrap-api-key"
SECRET_FILE = "session_secret"  # noqa: S105 - a file name
MIN_SECRET_BYTES = 32
SYSTEM = dto.Viewer("system", "agentlens", is_admin=True)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    project_id: str
    key_id: str
    plaintext: str | None = field(default=None, repr=False)  # None: configured key, never shown


def _data_dir(cfg: AgentlensConfig) -> Path:
    if cfg.server.data_dir is None:  # load_config always derives one
        raise ConfigError("server.data_dir", "effective configuration", "not set")
    return cfg.server.data_dir


async def first_run(
    store: StorageBackend,
    cfg: AgentlensConfig,
    *,
    clock_ns: Callable[[], int] = time.time_ns,
) -> BootstrapResult | None:
    """REQ-201: create ``default`` and its key when no Project exists; ``None`` otherwise."""
    if await store.list_projects():
        return None
    admin = AdminService(store, cfg, clock_ns=clock_ns)  # the caller skips demo mode
    try:
        project = await admin.create_project(SYSTEM, "Default", "default")
    except Conflict:  # another process bootstrapped first
        return None
    configured = cfg.bootstrap.api_key
    if configured is None:
        created = await admin.create_api_key(SYSTEM, project.id, "bootstrap")
        return BootstrapResult(project.id, created.key_id, created.plaintext.get_secret_value())
    key_id, secret = parse_api_key(configured.get_secret_value())
    hashed = hash_secret(secret)
    await store.create_api_key(
        NewApiKeyRecord(
            key_id=key_id,
            project_id=project.id,
            salt=hashed.salt,
            secret_hash=hashed.digest,
            created_at_ns=clock_ns(),
            label="bootstrap",
            created_by=SYSTEM.id,
        )
    )
    return BootstrapResult(project.id, key_id)


def _write_private(path: Path, text: str) -> None:
    """Create or replace ``path`` with mode 0600 before any byte is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)  # an existing file keeps its old mode otherwise
        fh.write(text)


def _base_url(cfg: AgentlensConfig) -> str:
    server = cfg.server
    if server.public_url is not None:
        return str(server.public_url).rstrip("/")
    return f"http://{server.http_host}:{server.http_port}"


def banner(
    cfg: AgentlensConfig, result: BootstrapResult | None, *, key_file: Path | None = None
) -> str:
    """API §8.3 first-run banner; only the first line when nothing was bootstrapped."""
    server = cfg.server
    base = _base_url(cfg)
    otlp = f"OTLP {base} (HTTP)"
    if server.ingest_enabled and server.grpc_enabled:
        otlp += f", {server.grpc_host or server.http_host}:{server.grpc_port} (gRPC)"
    lines = [f"agentlens {__version__} — UI {base}  ·  {otlp}"]
    if result is not None:
        if result.plaintext is None:
            lines.append('Created project "default" with the configured bootstrap API key.')
        elif key_file is not None:
            lines.append(f'Created project "default". API key written to {key_file} (mode 0600)')
        else:
            lines.append(f'Created project "default". API key (shown once):  {result.plaintext}')
            lines.append(f"  export OTEL_EXPORTER_OTLP_ENDPOINT={base}")
            lines.append(
                f'  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20{result.plaintext}"'
            )
    return "\n".join(lines) + "\n"


def announce(cfg: AgentlensConfig, result: BootstrapResult | None, stream: TextIO) -> None:
    """Write the banner to ``stream`` directly (never through ``logging``, REQ-201.a/323.a)."""
    key_file: Path | None = None
    if result is not None and result.plaintext is not None and not stream.isatty():
        key_file = _data_dir(cfg) / KEY_FILE
        _write_private(key_file, result.plaintext + "\n")
    stream.write(banner(cfg, result, key_file=key_file))
    with contextlib.suppress(Exception):
        stream.flush()


def session_secret(cfg: AgentlensConfig) -> bytes:
    """``auth.session_secret`` or the generated ``{data_dir}/session_secret`` (REQ-321)."""
    configured = cfg.auth.session_secret
    if configured is not None:
        value = configured.get_secret_value().encode()
        if len(value) < MIN_SECRET_BYTES:
            raise ConfigError("auth.session_secret", "effective configuration", "at least 32 bytes")
        return value
    path = _data_dir(cfg) / SECRET_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_text(encoding="utf-8").strip().encode()
        if len(value) < MIN_SECRET_BYTES:
            raise ConfigError(
                "auth.session_secret", str(path), "file holds fewer than 32 bytes"
            ) from None
        return value
    token = secrets.token_hex(MIN_SECRET_BYTES)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token.encode()
