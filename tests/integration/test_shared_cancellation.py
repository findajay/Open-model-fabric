"""Cancellation across replicas (``server.cancellation: shared``).

Two containers share one metadata database, like two API replicas behind a load
balancer. A cancel that reaches replica B must stop the stream served by replica A.
SQLite stands in for PostgreSQL here; CI also runs the metadata suite on PostgreSQL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from omf.bootstrap.config import parse_config
from omf.bootstrap.container import build_container
from omf.core.domain.events import StreamEventType
from omf.core.domain.models import ChatMessage, Role
from omf.core.services.inference import ChatCommand
from tests.conftest import base_config


async def test_cancel_on_one_replica_stops_a_stream_on_another(tmp_path: Path) -> None:
    raw = base_config(tmp_path, server={"cancellation": "shared"})
    raw["providers"][0]["config"] = {"first_token_delay_ms": 1, "token_delay_ms": 40}
    replica_a = await build_container(parse_config(raw, {}))
    replica_b = await build_container(parse_config(raw, {}))
    for replica in (replica_a, replica_b):
        await replica.start(background=False)
    try:
        cmd = ChatCommand(
            messages=[ChatMessage(role=Role.USER, content="a long answer please")],
            owner="alice",
        )
        events = []
        stream = replica_a.inference.stream(cmd)
        async for event in stream:
            events.append(event)
            if event.event == StreamEventType.DELTA and len(events) == 3:
                assert cmd.request_id in await replica_b.inference.inflight()
                # Another user cannot cancel it, even through the other replica.
                assert not await replica_b.inference.cancel(
                    cmd.request_id, owner="mallory", is_admin=False
                )
                assert await replica_b.inference.cancel(
                    cmd.request_id, owner="alice", is_admin=False
                )
        assert events[-1].event == StreamEventType.FAILED
        assert events[-1].data.error.code == "request_cancelled"  # type: ignore[union-attr]
        # Stopped by the poll, long before the ~40 tokens would have finished.
        assert sum(e.event == StreamEventType.DELTA for e in events) < 30
        await asyncio.sleep(0.05)
        assert await replica_b.inference.inflight() == []  # unregistered everywhere
        assert not await replica_b.inference.cancel(cmd.request_id)
    finally:
        await replica_a.aclose()
        await replica_b.aclose()


async def test_local_mode_does_not_touch_the_registry(tmp_path: Path) -> None:
    c = await build_container(parse_config(base_config(tmp_path), {}))
    await c.start(background=False)
    try:
        cmd = ChatCommand(messages=[ChatMessage(role=Role.USER, content="hi")])
        await c.inference.complete(cmd)
        assert await c.metadata.list_inflight() == []
        assert c.inference._poller is None
    finally:
        await c.aclose()
