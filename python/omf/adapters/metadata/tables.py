"""Table definitions (SQLAlchemy Core). Portable between SQLite and PostgreSQL.

Documents are stored as JSON text and timestamps as ISO-8601 UTC strings (which sort
lexicographically), avoiding dialect-specific JSON and timezone types. Columns used for
filtering or ordering are promoted out of the JSON document.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String, Table, Text

metadata = MetaData()

providers = Table(
    "providers",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("document", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

models = Table(
    "models",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("document", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("title", String(200), nullable=False),
    Column("owner", String(100), index=True),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False, index=True),
)

messages = Table(
    "messages",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "conversation_id",
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    Column("role", String(16), nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    Index("ix_messages_conversation_seq", "conversation_id", "seq", unique=True),
)

benchmarks = Table(
    "benchmarks",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("status", String(16), nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", String(40), nullable=False, index=True),
)

settings = Table(
    "settings",
    metadata,
    Column("key", String(100), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("actor", String(100), nullable=False),
    Column("action", String(100), nullable=False),
    Column("target", String(300), nullable=False),
    Column("request_id", String(100)),
    Column("details", Text, nullable=False),
    Column("created_at", String(40), nullable=False, index=True),
)

inference_records = Table(
    "inference_records",
    metadata,
    Column("request_id", String(100), primary_key=True),
    Column("provider_id", String(64)),
    Column("status", String(16), nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", String(40), nullable=False, index=True),
)
