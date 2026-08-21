"""From a batch to a queued reaction, through the real graph.

The sprint's definition of done for feat/echo-graph is that "oi" comes back as
a ✅. Every piece of that path is already covered on its own; this is the test
that they are actually connected, which is the thing unit tests never catch.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fittrack.graph.build import build_graph
from fittrack.graph.checkpoint import checkpointer, setup_checkpoint_tables
from fittrack.graph.runtime import GraphRunner
from fittrack.services.batch import Batch
from fittrack.services.outbound import OutboundQueue

pytestmark = pytest.mark.integration


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        self.jobs.append((function, args))
        return None


async def _tenant(conn: AsyncConnection, bsuid: str) -> int:
    row = await conn.execute(
        text("INSERT INTO tenant (bsuid, state) VALUES (:b, 'active') RETURNING id"),
        {"b": bsuid},
    )
    await conn.commit()
    return int(row.scalar_one())


async def test_oi_becomes_a_queued_acknowledgement(
    migrated: str, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    tenant_id = await _tenant(owner_conn, "e2e-oi")
    engine = create_async_engine(app_dsn)
    queue = OutboundQueue(engine)
    scheduler = FakeScheduler()

    await setup_checkpoint_tables(migrated)
    async with checkpointer(app_dsn) as saver:
        runner = GraphRunner(build_graph(checkpointer=saver), queue, scheduler)
        batch = Batch(
            id=1,
            tenant_id=tenant_id,
            combined_text="oi",
            message_ids=["wamid.oi"],
            attempts=1,
        )
        await runner.handle(batch, "e2e-oi")

    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None
    assert bubble.kind == "reaction"
    assert bubble.payload["emoji"] == "✅"
    assert bubble.payload["message_ids"] == ["wamid.oi"]

    # And something was told to send it. A bubble nobody delivers is the same
    # as no bubble, from where the user is sitting.
    assert [job[0] for job in scheduler.jobs] == ["deliver_outbound"]
    assert scheduler.jobs[0][1] == (tenant_id, "e2e-oi")


async def test_a_graph_that_says_nothing_queues_nothing(
    migrated: str, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Silence is a legitimate outcome (§7.3 ack_mode "silent"), and it must
    not leave an empty group blocking the queue behind it."""
    tenant_id = await _tenant(owner_conn, "e2e-silent")
    engine = create_async_engine(app_dsn)
    queue = OutboundQueue(engine)
    scheduler = FakeScheduler()

    class SilentGraph:
        async def ainvoke(
            self, state: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {"outbound": [], "errors": []}

    runner = GraphRunner(SilentGraph(), queue, scheduler)
    await runner.handle(
        Batch(id=2, tenant_id=tenant_id, combined_text="", message_ids=[], attempts=1),
        "e2e-silent",
    )

    assert await queue.claim_next(tenant_id) is None
    assert scheduler.jobs == []


async def test_a_retried_batch_does_not_queue_the_reply_twice(
    migrated: str, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The batch is retried when the worker dies after the enqueue commits.

    With a fresh group id per attempt, the same reply is inserted again and the
    user reads it twice -- and there is no signal anywhere that it happened,
    because both groups are perfectly valid.
    """
    tenant_id = await _tenant(owner_conn, "e2e-retry")
    engine = create_async_engine(app_dsn)
    queue = OutboundQueue(engine)

    await setup_checkpoint_tables(migrated)
    async with checkpointer(app_dsn) as saver:
        runner = GraphRunner(build_graph(checkpointer=saver), queue, FakeScheduler())
        batch = Batch(
            id=99,
            tenant_id=tenant_id,
            combined_text="oi",
            message_ids=["wamid.retry"],
            attempts=1,
        )
        await runner.handle(batch, "e2e-retry")
        # Same batch, second attempt.
        await runner.handle(replace(batch, attempts=2), "e2e-retry")

    first = await queue.claim_next(tenant_id)
    assert first is not None
    await queue.mark_sent(first, "wamid.sent")
    assert await queue.claim_next(tenant_id) is None, "the reply was queued twice"
