"""The SQL metadata store against SQLite and (when OMF_TEST_POSTGRES_URL is set) PostgreSQL."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from omf.adapters.metadata.sql import SqlMetadataStore
from omf.core.domain.capabilities import Capability, CapabilitySet
from omf.core.domain.errors import ConversationNotFound
from omf.core.domain.models import (
    AuditEntry,
    BenchmarkRun,
    BenchmarkStatus,
    BenchmarkTarget,
    ChatMessage,
    Conversation,
    GenerationParams,
    InferenceRecord,
    ModelDefinition,
    ProviderDefinition,
    RequestStatus,
    Role,
)

PG_URL = os.environ.get("OMF_TEST_POSTGRES_URL")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[SqlMetadataStore]:
    if request.param == "sqlite":
        s = SqlMetadataStore(f"sqlite:///{tmp_path}/meta.db")
        await s.initialize()
        yield s
        await s.aclose()
        return
    if not PG_URL:
        pytest.skip("OMF_TEST_POSTGRES_URL not set")
    schema = f"t_{uuid.uuid4().hex[:10]}"
    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in PG_URL else "?"
    s = SqlMetadataStore(f"{PG_URL}{sep}options=-csearch_path%3D{schema}")
    await s.initialize()
    yield s
    await s.aclose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


async def test_migrations_are_idempotent(store: SqlMetadataStore) -> None:
    await store.initialize()
    assert await store.ping()


async def test_providers_and_models(store: SqlMetadataStore) -> None:
    await store.upsert_provider(ProviderDefinition(id="p1", adapter_type="mock", config={"x": 1}))
    await store.upsert_provider(ProviderDefinition(id="p1", adapter_type="mock", config={"x": 2}))
    providers = await store.list_providers()
    assert len(providers) == 1 and providers[0].config == {"x": 2}
    assert await store.delete_provider("p1") and not await store.delete_provider("p1")

    model = ModelDefinition(
        id="org/m:1",
        display_name="M",
        capabilities=CapabilitySet.declared([Capability.CHAT], "test"),
    )
    await store.upsert_model(model)
    assert (await store.get_model("org/m:1")) == model
    assert await store.get_model("nope") is None


async def test_conversations_keep_order(store: SqlMetadataStore) -> None:
    conv = Conversation(title="t")
    await store.create_conversation(conv)
    for i in range(3):
        await store.append_messages(
            conv.id,
            [
                ChatMessage(role=Role.USER, content=f"q{i}"),
                ChatMessage(role=Role.ASSISTANT, content=f"a{i}"),
            ],
        )
    loaded = await store.get_conversation(conv.id)
    assert loaded is not None
    assert [m.text() for m in loaded.messages] == ["q0", "a0", "q1", "a1", "q2", "a2"]
    await store.rename_conversation(conv.id, "renamed")
    assert (await store.list_conversations())[0].title == "renamed"
    with pytest.raises(ConversationNotFound):
        await store.append_messages("missing", [])
    assert await store.delete_conversation(conv.id)
    assert await store.get_conversation(conv.id) is None


async def test_benchmarks_settings_audit_records(store: SqlMetadataStore) -> None:
    run = BenchmarkRun(
        name="b",
        dataset_id="smoke",
        dataset_version="1",
        params=GenerationParams(),
        targets=[BenchmarkTarget(provider_id="mock", model="m")],
    )
    await store.save_benchmark(run)
    run.status = BenchmarkStatus.COMPLETED
    await store.save_benchmark(run)
    assert (await store.get_benchmark(run.id)).status == BenchmarkStatus.COMPLETED  # type: ignore[union-attr]
    await store.set_setting("k", {"nested": [1, 2]})
    assert await store.get_setting("k") == {"nested": [1, 2]}
    await store.append_audit(AuditEntry(actor="a", action="x", target="t", details={"d": 1}))
    assert (await store.list_audit())[0].details == {"d": 1}
    await store.add_record(
        InferenceRecord(
            request_id="r1",
            provider_id="p",
            model="m",
            status=RequestStatus.COMPLETED,
            streaming=False,
        )
    )
    assert (await store.recent_records())[0].request_id == "r1"


async def test_migration_0002_upgrades_an_existing_0001_database(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from omf.adapters.metadata.sql import MIGRATIONS

    url = f"sqlite:///{tmp_path}/old.db"
    engine = create_engine(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
        ts = "2026-01-01T00:00:00+00:00"
        conn.execute(text(f"INSERT INTO conversations VALUES ('c1', 'old', '{ts}', '{ts}')"))
    engine.dispose()
    store = SqlMetadataStore(url)
    await store.initialize()  # applies 0002 on top of existing data
    old = await store.get_conversation("c1")
    assert old is not None and old.owner is None
    await store.aclose()
