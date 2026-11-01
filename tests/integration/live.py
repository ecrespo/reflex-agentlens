"""Live ingest servers in subprocesses, a real database, and a cuttable TCP proxy."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.security import generate_api_key, hash_secret
from reflex_agentlens.store.postgres.backend import PostgresBackend
from reflex_agentlens.store.records import NewApiKeyRecord, NewProject, ProjectPatch
from reflex_agentlens.store.sqlite.backend import SqliteBackend

ROOT = Path(__file__).resolve().parents[2]
PB = "application/x-protobuf"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Database:
    """One test database: a SQLite file or a PostgreSQL schema, plus a backend for checks."""

    name: str
    url: str  # what the server connects to
    schema: str | None
    backend: SqliteBackend | PostgresBackend
    project_id: str = ""
    key: str = ""
    key_id: str = ""

    async def seed(self, patch: ProjectPatch | None = None) -> None:
        project = await self.backend.create_project(NewProject(new_ulid(), "live", "Live"))
        if patch is not None:
            await self.backend.update_project(project.id, patch)
        key = generate_api_key()
        h = hash_secret(key.secret)
        await self.backend.create_api_key(
            NewApiKeyRecord(key.key_id, project.id, h.salt, h.digest, time.time_ns())
        )
        self.project_id, self.key, self.key_id = project.id, key.plaintext, key.key_id

    async def count(self, table: str) -> int:
        b = self.backend
        engine = b.write_engine if isinstance(b, SqliteBackend) else b.engine
        async with engine.connect() as conn:
            return int((await conn.execute(text(f"select count(*) from {table}"))).scalar_one())  # noqa: S608


@asynccontextmanager
async def database(name: str, sqlite_template: Path, tmp_path: Path) -> AsyncIterator[Database]:
    if name == "sqlite":
        path = tmp_path / "live.db"
        shutil.copyfile(sqlite_template, path)
        lite = SqliteBackend(path)
        await lite.open()
        try:
            yield Database(name, f"sqlite:///{path}", None, lite)
        finally:
            await lite.close()
        return
    pg_url = os.environ["AGENTLENS_TEST_PG_URL"]
    schema = f"it_{secrets.token_hex(6)}"
    admin = create_async_engine(pg_url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    pg = PostgresBackend(pg_url, search_path=schema, pool_size=4)
    try:
        await pg.open()
        await pg.migrate()
        yield Database(name, pg_url, schema, pg)
    finally:
        await pg.close()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


class TcpProxy:
    """Forwards a local port to the database; :meth:`cut` makes the database unreachable."""

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.port = free_port()
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.target)
        except OSError:
            writer.close()
            return
        self._writers |= {writer, up_writer}

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        self._writers -= {writer, up_writer}

    async def cut(self) -> None:
        """Refuse new connections and reset the open ones (``wait_closed`` waits for them)."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for w in list(self._writers):
            w.transport.abort()
        self._writers.clear()
        if server is not None:
            await asyncio.wait_for(server.wait_closed(), 5)

    async def restore(self) -> None:
        await self.start()

    def url_for(self, url: str) -> str:
        parts = urlsplit(url)
        netloc = parts.netloc.rsplit("@", 1)
        host = f"127.0.0.1:{self.port}"
        new = f"{netloc[0]}@{host}" if len(netloc) == 2 else host
        return urlunsplit(parts._replace(netloc=new))


@dataclass
class LiveServer:
    db: Database
    http_port: int
    grpc_port: int
    process: subprocess.Popen[bytes]
    log: Path
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    async def post(
        self, body: bytes, *, path: str = "/v1/traces", key: str | None = None, **headers: str
    ) -> httpx.Response:
        hdrs = {"content-type": PB, **{k.replace("_", "-"): v for k, v in headers.items()}}
        hdrs["authorization"] = f"Bearer {self.db.key if key is None else key}"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            return await client.post(path, content=body, headers=hdrs)

    def kill(self) -> None:
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(10)


@asynccontextmanager
async def live_server(
    db: Database, tmp_path: Path, toml: str = "", *, db_url: str | None = None, name: str = "srv"
) -> AsyncIterator[LiveServer]:
    http_port, grpc_port = free_port(), free_port()
    config = tmp_path / f"{name}.toml"
    config.write_text(
        f'[server]\ngrpc_host = "127.0.0.1"\ngrpc_port = {grpc_port}\nshutdown_grace = "3s"\n'
        + toml
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AGENTLENS_IT_CONFIG": str(config),
        "AGENTLENS_IT_DB": db_url or db.url,
        "AGENTLENS_IT_SCHEMA": db.schema or "",
    }
    log = tmp_path / f"{name}.log"
    process = _spawn(http_port, env, log)
    server = LiveServer(db, http_port, grpc_port, process, log)
    try:
        await _wait_ready(server)
        yield server
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, 15)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 5)


def _spawn(http_port: int, env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    """Granian with one worker, like ``serve`` (sync: Popen returns at once)."""
    argv = [
        sys.executable, "-m", "granian", "--interface", "asgi", "--factory",
        "--host", "127.0.0.1", "--port", str(http_port), "--workers", "1",
        "--log-level", "warning", "tests.integration.ingest_app:create_app",
    ]  # fmt: skip
    with log.open("wb") as out:
        return subprocess.Popen(argv, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)


async def _wait_ready(server: LiveServer, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(base_url=server.base_url, timeout=2) as client:
        while time.monotonic() < deadline:
            if server.process.poll() is not None:
                raise RuntimeError(f"server exited: {server.log.read_text()[-2000:]}")
            with contextlib.suppress(httpx.HTTPError):
                if (await client.get("/it/ready")).status_code == 200:
                    return
            await asyncio.sleep(0.1)
    raise TimeoutError(f"server not ready: {server.log.read_text()[-2000:]}")
