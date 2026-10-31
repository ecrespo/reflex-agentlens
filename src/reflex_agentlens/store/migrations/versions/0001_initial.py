"""Initial schema: Data Model v1.1 §2.1-2.18 (plan task P2-02.1).

Creates every table from the frozen snapshot ``snapshots.schema_0001`` (never from the live
``store.schema``), ``span_search`` per backend, and the ``app_meta`` rows ``instance_id`` and
``created_at_ns``. No pricing seed: bundled pricing is saved at bootstrap (Analyze A-11).

Revision ID: 0001
Revises:
Create Date: 2026-10-31
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import insert

from reflex_agentlens.core.ids import new_ulid
from reflex_agentlens.core.timeutil import now_ns
from reflex_agentlens.store.migrations.snapshots import schema_0001 as snap

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    snap.metadata.create_all(bind)
    snap.create_search(bind)
    bind.execute(
        insert(snap.app_meta),
        [
            {"key": "instance_id", "value": new_ulid()},
            {"key": "created_at_ns", "value": str(now_ns())},
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    snap.drop_search(bind)
    snap.metadata.drop_all(bind)
