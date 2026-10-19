"""SQL metadata store (SQLite by default, PostgreSQL via ``postgresql+psycopg://``).

Uses synchronous SQLAlchemy Core in a worker thread: the metadata workload is small and
this keeps one code path for both databases. Schema changes go through Alembic
migrations (``migrations/versions``) which run on ``initialize()``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, delete, event, func, insert, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from omf.adapters.metadata import tables as t
from omf.core.domain.errors import ConversationNotFound, StorageError
from omf.core.domain.models import (
    AuditEntry,
    BenchmarkRun,
    ChatMessage,
    Conversation,
    InferenceRecord,
    ModelDefinition,
    ProviderDefinition,
    utcnow,
)
from omf.core.ports.metadata import MetadataStore

T = TypeVar("T")
MIGRATIONS = Path(__file__).parent / "migrations"


def _now() -> str:
    return utcnow().isoformat()


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class SqlMetadataStore(MetadataStore):
    def __init__(self, url: str) -> None:
        self._url = url
        if url.startswith("sqlite"):
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._engine: Engine = create_engine(url, connect_args={"check_same_thread": False})
            event.listen(self._engine, "connect", _sqlite_pragmas)
        else:
            self._engine = create_engine(url, pool_pre_ping=True)
        self._write_lock = asyncio.Lock()

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    async def _run(self, fn: Callable[[Connection], T], *, write: bool = False) -> T:
        def work() -> T:
            with self._engine.begin() as conn:
                return fn(conn)

        try:
            if write and self.dialect == "sqlite":
                async with self._write_lock:  # SQLite allows one writer at a time
                    return await asyncio.to_thread(work)
            return await asyncio.to_thread(work)
        except SQLAlchemyError as exc:
            raise StorageError(
                f"metadata store error: {type(exc).__name__}", retryable=True
            ) from exc

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        def migrate() -> None:
            cfg = AlembicConfig()
            cfg.set_main_option("script_location", str(MIGRATIONS))
            with self._engine.begin() as conn:
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")

        await asyncio.to_thread(migrate)

    async def ping(self) -> bool:
        try:
            await self._run(lambda c: c.execute(text("SELECT 1")).scalar())
        except StorageError:
            return False
        return True

    async def aclose(self) -> None:
        await asyncio.to_thread(self._engine.dispose)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _upsert(
        conn: Connection, table: Any, key_col: str, key: str, values: dict[str, Any]
    ) -> None:
        exists = conn.execute(select(table.c[key_col]).where(table.c[key_col] == key)).first()
        if exists:
            conn.execute(update(table).where(table.c[key_col] == key).values(**values))
        else:
            conn.execute(insert(table).values(**{key_col: key, **values}))

    # ------------------------------------------------------------------ providers

    async def list_providers(self) -> list[ProviderDefinition]:
        rows = await self._run(lambda c: c.execute(select(t.providers.c.document)).scalars().all())
        return [ProviderDefinition.model_validate_json(r) for r in rows]

    async def get_provider(self, provider_id: str) -> ProviderDefinition | None:
        row = await self._run(
            lambda c: c.execute(
                select(t.providers.c.document).where(t.providers.c.id == provider_id)
            ).scalar()
        )
        return ProviderDefinition.model_validate_json(row) if row else None

    async def upsert_provider(self, provider: ProviderDefinition) -> None:
        values = {"document": provider.model_dump_json(), "updated_at": _now()}
        await self._run(
            lambda c: self._upsert(c, t.providers, "id", provider.id, values), write=True
        )

    async def delete_provider(self, provider_id: str) -> bool:
        n = await self._run(
            lambda c: (
                c.execute(delete(t.providers).where(t.providers.c.id == provider_id)).rowcount
            ),
            write=True,
        )
        return bool(n)

    # ------------------------------------------------------------------ models

    async def list_models(self) -> list[ModelDefinition]:
        rows = await self._run(lambda c: c.execute(select(t.models.c.document)).scalars().all())
        return [ModelDefinition.model_validate_json(r) for r in rows]

    async def get_model(self, model_id: str) -> ModelDefinition | None:
        row = await self._run(
            lambda c: c.execute(
                select(t.models.c.document).where(t.models.c.id == model_id)
            ).scalar()
        )
        return ModelDefinition.model_validate_json(row) if row else None

    async def upsert_model(self, model: ModelDefinition) -> None:
        values = {"document": model.model_dump_json(), "updated_at": _now()}
        await self._run(lambda c: self._upsert(c, t.models, "id", model.id, values), write=True)

    async def delete_model(self, model_id: str) -> bool:
        n = await self._run(
            lambda c: c.execute(delete(t.models).where(t.models.c.id == model_id)).rowcount,
            write=True,
        )
        return bool(n)

    # ------------------------------------------------------------------ conversations

    async def create_conversation(self, conversation: Conversation) -> None:
        def work(c: Connection) -> None:
            c.execute(
                insert(t.conversations).values(
                    id=conversation.id,
                    title=conversation.title,
                    created_at=conversation.created_at.isoformat(),
                    updated_at=conversation.updated_at.isoformat(),
                )
            )
            self._insert_messages(c, conversation.id, conversation.messages, start=0)

        await self._run(work, write=True)

    @staticmethod
    def _insert_messages(
        c: Connection, conversation_id: str, msgs: list[ChatMessage], start: int
    ) -> None:
        for offset, m in enumerate(msgs):
            c.execute(
                insert(t.messages).values(
                    id=m.id,
                    conversation_id=conversation_id,
                    seq=start + offset,
                    role=m.role.value,
                    document=m.model_dump_json(),
                    created_at=m.created_at.isoformat(),
                )
            )

    async def get_conversation(
        self, conversation_id: str, *, include_messages: bool = True
    ) -> Conversation | None:
        def work(c: Connection) -> Conversation | None:
            row = (
                c.execute(select(t.conversations).where(t.conversations.c.id == conversation_id))
                .mappings()
                .first()
            )
            if row is None:
                return None
            msgs: list[ChatMessage] = []
            if include_messages:
                docs = (
                    c.execute(
                        select(t.messages.c.document)
                        .where(t.messages.c.conversation_id == conversation_id)
                        .order_by(t.messages.c.seq)
                    )
                    .scalars()
                    .all()
                )
                msgs = [ChatMessage.model_validate_json(d) for d in docs]
            return Conversation(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                messages=msgs,
            )

        return await self._run(work)

    async def list_conversations(self, *, limit: int = 50) -> list[Conversation]:
        rows = await self._run(
            lambda c: (
                c.execute(
                    select(t.conversations)
                    .order_by(t.conversations.c.updated_at.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        )
        return [
            Conversation(
                id=r["id"], title=r["title"], created_at=r["created_at"], updated_at=r["updated_at"]
            )
            for r in rows
        ]

    async def append_messages(self, conversation_id: str, messages: list[ChatMessage]) -> None:
        def work(c: Connection) -> None:
            exists = c.execute(
                select(t.conversations.c.id).where(t.conversations.c.id == conversation_id)
            ).first()
            if not exists:
                raise ConversationNotFound(f"conversation {conversation_id!r} not found")
            start = (
                c.execute(
                    select(func.coalesce(func.max(t.messages.c.seq), -1)).where(
                        t.messages.c.conversation_id == conversation_id
                    )
                ).scalar_one()
                + 1
            )
            self._insert_messages(c, conversation_id, messages, start=start)
            c.execute(
                update(t.conversations)
                .where(t.conversations.c.id == conversation_id)
                .values(updated_at=_now())
            )

        await self._run(work, write=True)

    async def rename_conversation(self, conversation_id: str, title: str) -> None:
        n = await self._run(
            lambda c: (
                c.execute(
                    update(t.conversations)
                    .where(t.conversations.c.id == conversation_id)
                    .values(title=title, updated_at=_now())
                ).rowcount
            ),
            write=True,
        )
        if not n:
            raise ConversationNotFound(f"conversation {conversation_id!r} not found")

    async def delete_conversation(self, conversation_id: str) -> bool:
        def work(c: Connection) -> int:
            c.execute(delete(t.messages).where(t.messages.c.conversation_id == conversation_id))
            return int(
                c.execute(
                    delete(t.conversations).where(t.conversations.c.id == conversation_id)
                ).rowcount
            )

        return bool(await self._run(work, write=True))

    # ------------------------------------------------------------------ benchmarks

    async def save_benchmark(self, run: BenchmarkRun) -> None:
        values = {
            "name": run.name,
            "status": run.status.value,
            "document": run.model_dump_json(),
            "created_at": run.created_at.isoformat(),
        }
        await self._run(lambda c: self._upsert(c, t.benchmarks, "id", run.id, values), write=True)

    async def get_benchmark(self, run_id: str) -> BenchmarkRun | None:
        row = await self._run(
            lambda c: c.execute(
                select(t.benchmarks.c.document).where(t.benchmarks.c.id == run_id)
            ).scalar()
        )
        return BenchmarkRun.model_validate_json(row) if row else None

    async def list_benchmarks(self, *, limit: int = 50) -> list[BenchmarkRun]:
        rows = await self._run(
            lambda c: (
                c.execute(
                    select(t.benchmarks.c.document)
                    .order_by(t.benchmarks.c.created_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        )
        return [BenchmarkRun.model_validate_json(r) for r in rows]

    # ------------------------------------------------------------------ settings

    async def get_setting(self, key: str) -> Any | None:
        row = await self._run(
            lambda c: c.execute(select(t.settings.c.value).where(t.settings.c.key == key)).scalar()
        )
        return json.loads(row) if row is not None else None

    async def set_setting(self, key: str, value: Any) -> None:
        values = {"value": json.dumps(value), "updated_at": _now()}
        await self._run(lambda c: self._upsert(c, t.settings, "key", key, values), write=True)

    async def all_settings(self) -> dict[str, Any]:
        rows = await self._run(
            lambda c: c.execute(select(t.settings.c.key, t.settings.c.value)).all()
        )
        return {k: json.loads(v) for k, v in rows}

    # ------------------------------------------------------------------ audit

    async def append_audit(self, entry: AuditEntry) -> None:
        await self._run(
            lambda c: c.execute(
                insert(t.audit_log).values(
                    id=entry.id,
                    actor=entry.actor,
                    action=entry.action,
                    target=entry.target,
                    request_id=entry.request_id,
                    details=json.dumps(entry.details),
                    created_at=entry.created_at.isoformat(),
                )
            ),
            write=True,
        )

    async def list_audit(self, *, limit: int = 100) -> list[AuditEntry]:
        rows = await self._run(
            lambda c: (
                c.execute(
                    select(t.audit_log).order_by(t.audit_log.c.created_at.desc()).limit(limit)
                )
                .mappings()
                .all()
            )
        )
        return [
            AuditEntry(
                id=r["id"],
                actor=r["actor"],
                action=r["action"],
                target=r["target"],
                request_id=r["request_id"],
                details=json.loads(r["details"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ inference records

    async def add_record(self, record: InferenceRecord) -> None:
        values = {
            "provider_id": record.provider_id,
            "status": record.status.value,
            "document": record.model_dump_json(),
            "created_at": record.created_at.isoformat(),
        }
        await self._run(
            lambda c: self._upsert(c, t.inference_records, "request_id", record.request_id, values),
            write=True,
        )

    async def recent_records(self, *, limit: int = 50) -> list[InferenceRecord]:
        rows = await self._run(
            lambda c: (
                c.execute(
                    select(t.inference_records.c.document)
                    .order_by(t.inference_records.c.created_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        )
        return [InferenceRecord.model_validate_json(r) for r in rows]
