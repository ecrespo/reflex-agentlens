"""The ``StorageBackend`` protocol (API §5 v1.2; plan task P2-03.1; REQ-070.a).

The protocol is async and fully typed. It grows **with the method-ownership table** of the P2
Tasks (Analyze A-06): a method is declared here by the task that implements and contract-tests
it, together with its record types, so the protocol never names a type or a method that no
backend provides yet. The authoritative full list remains API §5.

Implemented so far:

- P2-05.1 / P2-06.1: ``open``, ``close``, ``ping``
- P2-02.2: ``schema_status``, ``migrate``
- P2-05.2 / P2-06.1: ``write_batch``
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from reflex_agentlens.store.records import NormalizedBatch, SchemaStatus, WriteResult

__all__ = ["StorageBackend"]


@runtime_checkable
class StorageBackend(Protocol):
    """A storage backend. Every implementation passes ``tests/contract/`` (REQ-070.b)."""

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Connect pools and check the database version. Never migrates implicitly."""

    async def close(self) -> None:
        """Release every connection; idempotent."""

    async def ping(self) -> bool:
        """``True`` when the database answers (REQ-330, REQ-340); never raises."""

    async def schema_status(self) -> SchemaStatus:
        """``CURRENT``, ``OUTDATED`` or ``NEWER`` (REQ-074)."""

    async def migrate(self) -> None:
        """Alembic upgrade to head, under a lock where several processes may race (REQ-074.d)."""

    # ── ingest path ──────────────────────────────────────────────────────────────────────────

    async def write_batch(self, batch: NormalizedBatch) -> WriteResult:
        """Persist one normalized batch in one transaction (API §5.1)."""
