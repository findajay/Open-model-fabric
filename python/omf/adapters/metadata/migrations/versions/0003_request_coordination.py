"""Shared in-flight registry and cancel requests (server.cancellation: shared).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inflight_requests",
        sa.Column("request_id", sa.String(100), primary_key=True),
        sa.Column("owner", sa.String(130)),
        sa.Column("replica", sa.String(64), nullable=False),
        sa.Column("started_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_inflight_requests_started_at", "inflight_requests", ["started_at"])
    op.create_table(
        "cancel_requests",
        sa.Column("request_id", sa.String(100), primary_key=True),
        sa.Column("requested_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cancel_requests")
    op.drop_index("ix_inflight_requests_started_at", "inflight_requests")
    op.drop_table("inflight_requests")
