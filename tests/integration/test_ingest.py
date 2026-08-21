"""Persistence of an inbound delivery (§4.1, §5.2)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fittrack.channels.whatsapp.ingest import Ingest
from fittrack.channels.whatsapp.payload import InboundMessage
from fittrack.crypto.aesgcm import Encryptor, KeyRing
from fittrack.services.outbound import Bubble, OutboundQueue
from fittrack.services.retry_policy import classify

pytestmark = pytest.mark.integration

ENCRYPTOR = Encryptor(KeyRing({1: os.urandom(32)}, current_version=1))
WINDOW = 10


class FakeBuffer:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, dict[str, Any]]] = []

    async def push(self, bsuid: str, message: dict[str, Any]) -> None:
        self.pushed.append((bsuid, message))


class FakeScheduler:
    def __init__(self, fails: bool = False) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._fails = fails

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        if self._fails:
            raise ConnectionError("queue is down")
        self.jobs.append((function, args, kwargs))
        return None


def _message(message_id: str = "wamid.1", **kw: Any) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        bsuid=kw.pop("bsuid", "BSUID-ingest"),
        msg_type=kw.pop("msg_type", "text"),
        timestamp=1,
        text=kw.pop("text", "supino 80kg 8 reps"),
        **kw,
    )


@pytest.fixture
def ingest(app_dsn: str) -> tuple[Ingest, FakeBuffer, FakeScheduler]:
    """Connects as fittrack_app, the way production does.

    An earlier version used the owner engine, which is a superuser and bypasses
    RLS -- so the suite passed while every write would have been rejected in
    production. The whole point of this fixture is that the policies are live.
    """
    engine = create_async_engine(app_dsn)
    buffer = FakeBuffer()
    scheduler = FakeScheduler()
    return Ingest(engine, ENCRYPTOR, buffer, scheduler, WINDOW), buffer, scheduler


async def test_first_contact_creates_the_tenant_before_the_message(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler], owner_conn: AsyncConnection
) -> None:
    """raw_message.tenant_id is NOT NULL ON DELETE CASCADE so an erasure cannot
    leave orphaned message bodies (§5.2). That makes the upsert order a
    correctness requirement, not a preference."""
    service, _, _scheduler = ingest

    await service.accept_message(_message(), {"entry": []})

    row = await owner_conn.execute(
        text(
            "SELECT t.bsuid, r.msg_type FROM raw_message r "
            "JOIN tenant t ON t.id = r.tenant_id WHERE r.wa_message_id = 'wamid.1'"
        )
    )
    bsuid, msg_type = row.one()
    assert bsuid == "BSUID-ingest"
    assert msg_type == "text"


async def test_payload_is_stored_encrypted(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler], owner_conn: AsyncConnection
) -> None:
    service, _, _scheduler = ingest
    envelope = {"entry": [{"secret": "conteudo do usuario"}]}

    await service.accept_message(_message("wamid.enc"), envelope)

    row = await owner_conn.execute(
        text("SELECT payload FROM raw_message WHERE wa_message_id = 'wamid.enc'")
    )
    stored = bytes(row.scalar_one())
    assert b"conteudo do usuario" not in stored
    assert ENCRYPTOR.decrypt_json(stored) == envelope


async def test_redelivery_does_not_duplicate(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler], owner_conn: AsyncConnection
) -> None:
    """Meta redelivers whatever it did not get a 200 for. A redelivery reaching
    the buffer twice would double the workout volume."""
    service, buffer, _scheduler = ingest
    message = _message("wamid.dup")

    await service.accept_message(message, {"entry": []})
    await service.accept_message(message, {"entry": []})

    row = await owner_conn.execute(
        text("SELECT count(*) FROM raw_message WHERE wa_message_id = 'wamid.dup'")
    )
    assert row.scalar_one() == 1
    assert len(buffer.pushed) == 1


async def test_non_actionable_message_is_stored_but_not_queued(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler], owner_conn: AsyncConnection
) -> None:
    """A reaction is kept for audit and costs nothing downstream (§18.3)."""
    service, buffer, _scheduler = ingest

    await service.accept_message(
        _message("wamid.react", msg_type="reaction", text=None), {"entry": []}
    )

    row = await owner_conn.execute(
        text("SELECT count(*) FROM raw_message WHERE wa_message_id = 'wamid.react'")
    )
    assert row.scalar_one() == 1
    assert buffer.pushed == []


async def test_conversation_window_is_refreshed(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler], owner_conn: AsyncConnection
) -> None:
    """The 24h window decides whether the coach may send free text or must use
    a template (§14). It is anchored on the last inbound message."""
    service, _, _scheduler = ingest

    await service.accept_message(_message("wamid.win", bsuid="BSUID-window"), {})

    row = await owner_conn.execute(
        text(
            "SELECT last_inbound_at > now() - interval '1 minute' "
            "FROM conversation_window w JOIN tenant t ON t.id = w.tenant_id "
            "WHERE t.bsuid = 'BSUID-window'"
        )
    )
    assert row.scalar_one() is True


async def test_writes_succeed_under_row_level_security(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler], owner_conn: AsyncConnection
) -> None:
    """The regression this file exists for.

    raw_message and conversation_window are RLS-forced, and fittrack_app is not
    a superuser, so a transaction that does not SET LOCAL app.tenant_id has its
    writes rejected and the delivery is lost.
    """
    service, buffer, _scheduler = ingest

    await service.accept_message(_message("wamid.rls", bsuid="BSUID-rls"), {"e": 1})

    row = await owner_conn.execute(
        text(
            "SELECT count(*) FROM raw_message r JOIN tenant t ON t.id = r.tenant_id "
            "WHERE t.bsuid = 'BSUID-rls'"
        )
    )
    assert row.scalar_one() == 1
    assert len(buffer.pushed) == 1


async def test_a_failed_store_does_not_consume_the_delivery(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler], owner_conn: AsyncConnection
) -> None:
    """Deduplication is the unique constraint, claimed only once the row is
    durable. Claiming it in a cache beforehand meant a transient Postgres
    failure lost the workout permanently: Meta's retry found the id already
    claimed and returned without persisting anything.
    """
    service, buffer, _scheduler = ingest
    message = _message("wamid.retry", bsuid="BSUID-retry")

    await service.accept_message(message, {"e": 1})
    await service.accept_message(message, {"e": 1})

    row = await owner_conn.execute(
        text("SELECT count(*) FROM raw_message WHERE wa_message_id = 'wamid.retry'")
    )
    assert row.scalar_one() == 1
    assert len(buffer.pushed) == 1, "the redelivery must not reach the buffer twice"


async def test_a_buffered_message_schedules_its_own_flush(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler],
) -> None:
    """Buffering without scheduling is where a burst dies.

    Nothing else in the system enqueues `flush_user`: the worker only
    reschedules a job that already exists. Without this the messages sit in
    Redis until their TTL and the user never gets an answer.
    """
    service, buffer, scheduler = ingest
    await service.accept_message(_message("wamid.sched"), {"raw": True})

    assert buffer.pushed, "the message never reached the buffer"
    assert [job[0] for job in scheduler.jobs] == ["flush_user"]
    _, args, kwargs = scheduler.jobs[0]
    assert args == ("BSUID-ingest",)
    # After the window, not before: an earlier job finds the buffer not ready
    # and costs a whole round trip through the queue.
    assert kwargs["_defer_by"] > WINDOW


async def test_each_message_of_a_burst_schedules_a_flush(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler],
) -> None:
    """The window renews on every message, so the flush has to follow it.

    Scheduling once from the first message would pin the flush to a deadline
    that no longer exists and cut the burst in half.
    """
    service, _, scheduler = ingest
    for i in range(3):
        await service.accept_message(_message(f"wamid.burst{i}"), {"raw": True})

    assert len(scheduler.jobs) == 3


async def test_a_redelivery_does_not_schedule_a_second_flush(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler],
) -> None:
    """Meta retries deliveries it thinks failed. Those must not re-enter the
    buffer or the queue."""
    service, buffer, scheduler = ingest
    await service.accept_message(_message("wamid.dupe"), {"raw": True})
    await service.accept_message(_message("wamid.dupe"), {"raw": True})

    assert len(buffer.pushed) == 1
    assert len(scheduler.jobs) == 1


async def test_a_dead_queue_does_not_lose_the_delivery(app_dsn: str) -> None:
    """The message is already persisted and buffered by then.

    Raising here would make the webhook return 500 and Meta redeliver a
    message we already stored -- trading a recoverable delay for a duplicate.
    """
    buffer = FakeBuffer()
    service = Ingest(
        create_async_engine(app_dsn), ENCRYPTOR, buffer, FakeScheduler(fails=True), WINDOW
    )

    await service.accept_message(_message("wamid.noqueue"), {"raw": True})

    assert len(buffer.pushed) == 1


async def test_a_new_message_releases_bubbles_the_closed_window_parked(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The inbound message is the event that reopens the 24h window (§18.5).

    A bubble parked by a 131047 is waiting for exactly this and nothing else;
    on a timer alone it would sit there long after the window reopened.
    """
    engine = create_async_engine(app_dsn)
    queue = OutboundQueue(engine)
    scheduler = FakeScheduler()
    service = Ingest(engine, ENCRYPTOR, FakeBuffer(), scheduler, WINDOW, queue)

    # First contact creates the tenant.
    await service.accept_message(_message("wamid.win0", bsuid="BSUID-window"), {"raw": True})
    row = await owner_conn.execute(
        text("SELECT id FROM tenant WHERE bsuid = :b"), {"b": "BSUID-window"}
    )
    tenant_id = int(row.scalar_one())

    await queue.enqueue(tenant_id, [Bubble(kind="image", payload={"id": "m1"})])
    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None
    await queue.mark_failed(bubble, classify("131047"), "out of window", code="131047")
    assert await OutboundQueue(engine).claim_next(tenant_id) is None

    scheduler.jobs.clear()
    await service.accept_message(_message("wamid.win1", bsuid="BSUID-window"), {"raw": True})

    assert await OutboundQueue(engine).claim_next(tenant_id) is not None
    assert "deliver_outbound" in [job[0] for job in scheduler.jobs], (
        "the bubbles were released but nothing was told to send them"
    )


async def test_a_message_without_parked_bubbles_does_not_wake_the_dispatcher(
    ingest: tuple[Ingest, FakeBuffer, FakeScheduler],
) -> None:
    """Waking a dispatcher that has nothing to send is a job per message."""
    service, _, scheduler = ingest
    await service.accept_message(_message("wamid.nopark"), {"raw": True})

    assert [job[0] for job in scheduler.jobs] == ["flush_user"]
