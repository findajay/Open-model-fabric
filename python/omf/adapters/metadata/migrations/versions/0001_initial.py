"""Initial schema.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document", sa.Text, nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "models",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("document", sa.Text, nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("document", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_messages_conversation_seq", "messages", ["conversation_id", "seq"], unique=True
    )
    op.create_table(
        "benchmarks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("document", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_benchmarks_created_at", "benchmarks", ["created_at"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target", sa.String(300), nullable=False),
        sa.Column("request_id", sa.String(100)),
        sa.Column("details", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_table(
        "inference_records",
        sa.Column("request_id", sa.String(100), primary_key=True),
        sa.Column("provider_id", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("document", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_inference_records_created_at", "inference_records", ["created_at"])


def downgrade() -> None:
    for index, table in [
        ("ix_inference_records_created_at", "inference_records"),
        ("ix_audit_log_created_at", "audit_log"),
        ("ix_benchmarks_created_at", "benchmarks"),
        ("ix_messages_conversation_seq", "messages"),
        ("ix_conversations_updated_at", "conversations"),
    ]:
        op.drop_index(index, table_name=table)
    for table in [
        "inference_records",
        "audit_log",
        "settings",
        "benchmarks",
        "messages",
        "conversations",
        "models",
        "providers",
    ]:
        op.drop_table(table)
