"""The ``reflex-agentlens`` command (plan task P5-03; REQ-200, 202, API §8.3).

Exit codes (API §8.3): 0 success or clean shutdown · 1 runtime error · 2 usage or configuration
error · 3 port in use · 4 schema outdated (auto-migrate off) or newer than the code · 5 database
unreachable at startup. Messages go to stderr and never carry secrets: configuration errors name
the key and its source, and API keys are printed only by ``keys create`` (stdout, once).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TextIO

from reflex_agentlens import __version__
from reflex_agentlens.app.backend import Backend, Mode, check_mode, open_backend
from reflex_agentlens.app.lifespan import SchemaMismatch, prepare_schema
from reflex_agentlens.app.policy import check_bind
from reflex_agentlens.app.server import PortInUse, run_server
from reflex_agentlens.core.config import AgentlensConfig, format_size, load_config, render_config
from reflex_agentlens.core.errors import (
    ConfigError,
    Forbidden,
    NotFound,
    StorageUnavailable,
    ValidationError,
)
from reflex_agentlens.core.logging import configure_logging, reset_logging
from reflex_agentlens.core.semconv.constants import SEMCONV_CORE, SEMCONV_GENAI_COMMIT
from reflex_agentlens.core.semconv.mapping import MAPPING_VERSION
from reflex_agentlens.ingest.cost import bootstrap_pricing, load_bundled_pricing
from reflex_agentlens.ingest.jobs import Jobs
from reflex_agentlens.ingest.rederive import rederive
from reflex_agentlens.query import dto
from reflex_agentlens.query.admin import AdminService
from reflex_agentlens.query.timeexpr import resolve_time
from reflex_agentlens.store import migrate
from reflex_agentlens.store.records import RebuildScope, SchemaStatus

__all__ = ["EXIT", "build_parser", "main"]

EXIT = {"ok": 0, "runtime": 1, "usage": 2, "port": 3, "schema": 4, "database": 5}
CLI_VIEWER = dto.Viewer("cli", "command line", is_admin=True)
PROG = "reflex-agentlens"
log = logging.getLogger("agentlens.cli")


class UsageError(Exception):
    """A command-level usage problem (exit 2)."""


# ── parser ───────────────────────────────────────────────────────────────────────────────────


def _server_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", help="bind address (server.http_host)")
    p.add_argument("--port", type=int, help="HTTP port: UI and OTLP/HTTP (server.http_port)")
    p.add_argument("--grpc-port", type=int, help="OTLP/gRPC port (server.grpc_port)")
    p.add_argument("--no-grpc", action="store_true", help="do not start the OTLP/gRPC receiver")
    p.add_argument("--print-config", action="store_true", help="print the effective configuration")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Self-hosted observability for AI agents."
    )
    parser.add_argument("--config", metavar="PATH", help="agentlens.toml to read")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    _server_options(sub.add_parser("serve", help="UI + OTLP ingest in one process (default)"))
    _server_options(sub.add_parser("collect", help="OTLP ingest only (PostgreSQL)"))
    demo = sub.add_parser("demo", help="serve the bundled sample dataset, read-only")
    demo.add_argument("--print-config", action="store_true", help=argparse.SUPPRESS)

    db = sub.add_parser("db", help="database schema")
    db_sub = db.add_subparsers(dest="db_command", metavar="ACTION", required=True)
    db_sub.add_parser("upgrade", help="apply migrations and exit")
    db_sub.add_parser("status", help="print CURRENT | OUTDATED | NEWER and the revision IDs")

    rebuild = sub.add_parser("rebuild", help="recompute derived views from stored spans")
    rebuild.add_argument("--project", metavar="SLUG", action="append", help="limit to a Project")
    rebuild.add_argument("--since", metavar="TIME", help="RFC 3339 or now-<n><s|m|h|d|w>")

    purge = sub.add_parser("purge", help="run retention once")
    purge.add_argument("--dry-run", action="store_true", help="report without deleting")

    keys = sub.add_parser("keys", help="API keys")
    keys_sub = keys.add_subparsers(dest="keys_command", metavar="ACTION", required=True)
    create = keys_sub.add_parser("create", help="create a key and print it once")
    create.add_argument("--project", metavar="SLUG", required=True)
    create.add_argument("--label", metavar="TEXT", default="")
    listing = keys_sub.add_parser("list", help="list a Project's keys")
    listing.add_argument("--project", metavar="SLUG", required=True)
    revoke = keys_sub.add_parser("revoke", help="revoke a key")
    revoke.add_argument("key_id", metavar="KEY_ID")

    sub.add_parser("version", help="print package, OTLP proto and semconv mapping versions")
    return parser


# ── configuration ────────────────────────────────────────────────────────────────────────────


def _overrides(args: argparse.Namespace) -> dict[str, object]:
    flags = {
        "server.http_host": getattr(args, "host", None),
        "server.http_port": getattr(args, "port", None),
        "server.grpc_port": getattr(args, "grpc_port", None),
    }
    out: dict[str, object] = {k: v for k, v in flags.items() if v is not None}
    if getattr(args, "no_grpc", False):
        out["server.grpc_enabled"] = False
    if args.command == "demo":
        out["demo.enabled"] = True
    return out


def _config(args: argparse.Namespace, env: Mapping[str, str] | None) -> AgentlensConfig:
    defaults: dict[str, object] = {}
    if args.command == "collect":
        defaults["server.http_port"] = 4318  # API §8.2: 8000 for serve, 4318 for collect
    return load_config(
        config_path=args.config,
        env=env,
        cli_overrides=_overrides(args),
        command_defaults=defaults,
    )


# ── one-shot commands ────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _store(cfg: AgentlensConfig) -> AsyncIterator[Backend]:
    """A backend with a usable schema (auto-migrate honoured) and the pricing seed."""
    backend = await open_backend(cfg)
    try:
        await prepare_schema(backend, cfg)
        await bootstrap_pricing(backend, load_bundled_pricing(cfg.pricing.file))
        yield backend
    finally:
        await backend.close()


async def _project_id(backend: Backend, slug: str) -> str:
    for project in await backend.list_projects():
        if project.slug == slug:
            return project.id
    raise NotFound(f"project {slug!r}")


async def _db(cfg: AgentlensConfig, action: str, out: TextIO) -> int:
    backend = await open_backend(cfg)
    try:
        if action == "upgrade":
            status = await backend.schema_status()
            if status is SchemaStatus.NEWER:
                raise SchemaMismatch(
                    "the database schema is newer than this agentlens: downgrade the database "
                    "or upgrade agentlens"
                )
            await backend.migrate()
            out.write(f"schema: current ({migrate.head_revision()})\n")
            return EXIT["ok"]
        status = await backend.schema_status()
        async with backend.write_engine.connect() as conn:
            current = await conn.run_sync(migrate.current_revision)
        out.write(f"{status.value.upper()}\n")
        out.write(
            f"database revision: {current or 'none'}\ncode revision: {migrate.head_revision()}\n"
        )
        return EXIT["ok"]
    finally:
        await backend.close()


async def _keys(cfg: AgentlensConfig, args: argparse.Namespace, out: TextIO) -> int:
    async with _store(cfg) as backend:
        admin = AdminService(backend, cfg)
        if args.keys_command == "revoke":
            await admin.revoke_api_key(CLI_VIEWER, args.key_id)
            out.write(f"revoked {args.key_id}\n")
            return EXIT["ok"]
        project_id = await _project_id(backend, args.project)
        if args.keys_command == "create":
            created = await admin.create_api_key(CLI_VIEWER, project_id, args.label)
            out.write(f"key_id: {created.key_id}\n")
            out.write(f"API key (shown once): {created.plaintext.get_secret_value()}\n")
            out.write(created.snippet)
            return EXIT["ok"]
        keys = await admin.list_api_keys(CLI_VIEWER, project_id)
        out.write("KEY_ID    PREFIX        LABEL                 CREATED               STATE\n")
        for key in keys:
            state = "revoked" if key.revoked else "active"
            created_at = _when(key.created_at_ns)
            out.write(
                f"{key.key_id:<9} {key.prefix:<13} {key.label[:21]:<21} {created_at:<21} {state}\n"
            )
        return EXIT["ok"]


def _when(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _purge(cfg: AgentlensConfig, dry_run: bool, out: TextIO) -> int:
    retention = cfg.retention
    async with _store(cfg) as backend:
        now = time.time_ns()
        if dry_run:
            expired = await backend.count_purgeable(now)
            out.write(f"would delete {expired} traces past their Project's maximum age\n")
            limit = retention.storage_size_limit
            usage = await backend.storage_usage()
            if limit is not None and usage.estimated_bytes > limit:
                excess = format_size(usage.estimated_bytes - limit)
                out.write(f"storage is {excess} above the size limit; a purge frees it\n")
            return EXIT["ok"]
        jobs = Jobs(
            backend,
            finalization_window_s=cfg.ingest.finalization_window.total_seconds(),
            purge_batch_traces=retention.purge_batch_traces,
            storage_size_limit=retention.storage_size_limit,
        )
        reports = await jobs.purge(trigger="manual")
        if reports is None:
            out.write("another process is purging; nothing done\n")
            return EXIT["ok"]
        age = sum(r.deleted_traces_age for r in reports)
        size = sum(r.deleted_traces_size for r in reports)
        freed = format_size(sum(r.deleted_bytes_est for r in reports))
        out.write(f"deleted {age} traces by age and {size} by size (~{freed})\n")
        return EXIT["ok"]


async def _rebuild(cfg: AgentlensConfig, args: argparse.Namespace, out: TextIO) -> int:
    async with _store(cfg) as backend:
        projects = None
        if args.project:
            projects = frozenset([await _project_id(backend, slug) for slug in args.project])
        since = None if args.since is None else resolve_time(args.since, time.time_ns(), "since")
        pricing = await backend.get_pricing()
        report = await backend.rebuild(RebuildScope(projects, since), rederive, pricing)
        out.write(
            f"Runs scanned: {report.runs_scanned}, spans re-derived: {report.spans_rederived}, "
            f"Runs changed: {report.runs_changed}\n"
        )
        return EXIT["ok"]


def _version(out: TextIO) -> int:
    proto = importlib.metadata.version("opentelemetry-proto")
    out.write(f"{PROG} {__version__}\n")
    out.write(f"OTLP proto {proto}\n")
    out.write(
        f"semconv mapping {MAPPING_VERSION} (semconv {SEMCONV_CORE}, "
        f"GenAI {SEMCONV_GENAI_COMMIT[:7]})\n"
    )
    return EXIT["ok"]


# ── dispatch ─────────────────────────────────────────────────────────────────────────────────


def _dispatch(args: argparse.Namespace, cfg: AgentlensConfig, out: TextIO) -> int:
    command: str = args.command
    if command in ("serve", "collect", "demo"):
        mode: Mode = command  # type: ignore[assignment]
        check_mode(cfg, mode)
        if mode != "collect":
            check_bind(cfg)
        if mode == "demo":
            raise UsageError("demo: the bundled sample dataset ships with plan task P8-02")
        asyncio.run(run_server(cfg, mode, banner=out))
        return EXIT["ok"]
    if command == "db":
        return asyncio.run(_db(cfg, args.db_command, out))
    if command == "keys":
        return asyncio.run(_keys(cfg, args, out))
    if command == "purge":
        return asyncio.run(_purge(cfg, args.dry_run, out))
    if command == "rebuild":
        return asyncio.run(_rebuild(cfg, args, out))
    raise UsageError(f"unknown command {command!r}")


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI and return the process exit code."""
    out, err = stdout or sys.stdout, stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:  # --help (0) or a usage error (2), already printed by argparse
        return exc.code if isinstance(exc.code, int) else EXIT["usage"]
    if args.command is None:
        args = parser.parse_args(
            ["serve"] if args.config is None else ["--config", args.config, "serve"]
        )
    if args.command == "version":
        return _version(out)

    def fail(code: str, message: str) -> int:
        err.write(f"{PROG}: {message}\n")
        return EXIT[code]

    try:
        cfg = _config(args, env)
        if getattr(args, "print_config", False):
            out.write(render_config(cfg))
            return EXIT["ok"]
        long_running = args.command in ("serve", "collect", "demo")
        configure_logging(
            cfg.logging.level, cfg.logging.format, stream=out if long_running else err
        )
        try:
            return _dispatch(args, cfg, out)
        finally:
            reset_logging()
    except ConfigError as exc:
        return fail("usage", str(exc))
    except (UsageError, NotFound, ValidationError, Forbidden) as exc:
        return fail("usage", str(exc))
    except PortInUse as exc:
        return fail("port", str(exc))
    except SchemaMismatch as exc:
        return fail("schema", str(exc))
    except StorageUnavailable:
        return fail("database", "database unreachable (check database.url and that it is running)")
    except KeyboardInterrupt:
        return EXIT["ok"]
    except Exception as exc:
        log.exception("command failed")
        return fail("runtime", f"runtime error ({type(exc).__name__})")


if __name__ == "__main__":
    raise SystemExit(main())
