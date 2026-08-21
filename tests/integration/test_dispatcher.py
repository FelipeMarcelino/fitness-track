"""The queue and a channel together (§18.5)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fittrack.channels.base import SendError
from fittrack.services.dispatcher import Dispatcher
from fittrack.services.outbound import Bubble, OutboundQueue

pytestmark = pytest.mark.integration


class FakeChannel:
    """Records what went out and fails on demand."""

    def __init__(self, fail_on: dict[int, SendError] | None = None) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        # Keyed on call number, not on successful sends. Keying on `sent`
        # would make every call after the first failure fail too, which hides
        # a dispatcher that carries on past a failure.
        self.calls = 0
        self._fail_on = fail_on or {}

    async def send(self, bsuid: str, kind: str, payload: dict[str, Any]) -> str:
        failure = self._fail_on.get(self.calls)
        self.calls += 1
        if failure is not None:
            raise failure
        self.sent.append((bsuid, kind, payload))
        return f"wamid.{len(self.sent)}"


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        self.jobs.append((function, args, kwargs))
        return None


async def _tenant(conn: AsyncConnection, bsuid: str) -> int:
    row = await conn.execute(
        text("INSERT INTO tenant (bsuid, state) VALUES (:b, 'active') RETURNING id"),
        {"b": bsuid},
    )
    await conn.commit()
    return int(row.scalar_one())


def _bubbles(*texts: str) -> list[Bubble]:
    return [Bubble(kind="text", payload={"body": body}) for body in texts]


async def test_a_whole_reply_goes_out_in_order(app_dsn: str, owner_conn: AsyncConnection) -> None:
    tenant_id = await _tenant(owner_conn, "disp-order")
    queue = OutboundQueue(create_async_engine(app_dsn))
    await queue.enqueue(tenant_id, _bubbles("um", "dois", "tres"))
    channel = FakeChannel()

    assert await Dispatcher(queue, channel).deliver(tenant_id, "disp-order") == 3
    assert [payload["body"] for _, _, payload in channel.sent] == ["um", "dois", "tres"]


async def test_the_rest_of_a_reply_stays_put_when_one_bubble_fails(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Sending bubble 3 after bubble 2 failed delivers a reply with a hole in
    it, and nothing tells the user a piece is missing.

    The queue is what enforces this -- seq n+1 is ineligible until seq n has
    sent_at -- so this pins the eligibility rule, not the dispatcher loop.
    """
    tenant_id = await _tenant(owner_conn, "disp-stop")
    queue = OutboundQueue(create_async_engine(app_dsn))
    await queue.enqueue(tenant_id, _bubbles("um", "dois", "tres"))
    channel = FakeChannel(fail_on={1: SendError("rate limited", code="130429", status=400)})

    sent = await Dispatcher(queue, channel).deliver(tenant_id, "disp-stop")

    assert sent == 1
    assert [payload["body"] for _, _, payload in channel.sent] == ["um"]


async def test_a_retryable_failure_wakes_the_tenant_again(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The row carries its own next_retry_at, but a row nobody comes back for
    is a message nobody sends."""
    tenant_id = await _tenant(owner_conn, "disp-wake")
    queue = OutboundQueue(create_async_engine(app_dsn))
    await queue.enqueue(tenant_id, _bubbles("depois"))
    channel = FakeChannel(fail_on={0: SendError("rate limited", code="130429", status=400)})
    scheduler = FakeScheduler()

    await Dispatcher(queue, channel, scheduler).deliver(tenant_id, "disp-wake")

    assert [job[0] for job in scheduler.jobs] == ["deliver_outbound"]
    _, args, kwargs = scheduler.jobs[0]
    assert args == (tenant_id, "disp-wake")
    assert kwargs["_defer_by"] > 0


async def test_a_permanent_failure_is_not_rescheduled(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Waking up for a message that can never be sent is how a dead letter
    turns into an infinite loop."""
    tenant_id = await _tenant(owner_conn, "disp-dead")
    queue = OutboundQueue(create_async_engine(app_dsn))
    group = await queue.enqueue(tenant_id, _bubbles("ruim", "pior"))
    channel = FakeChannel(fail_on={0: SendError("invalid parameter", code="100", status=400)})
    scheduler = FakeScheduler()

    await Dispatcher(queue, channel, scheduler).deliver(tenant_id, "disp-dead")

    assert scheduler.jobs == []
    rows = await queue.group(tenant_id, group)
    assert all(row.dead_at is not None for row in rows)


async def test_a_timeout_is_retried_rather_than_given_up_on(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """A timeout carries no code and no status: we do not know whether the
    message went out, which §18.5 treats as the one genuinely transient case."""
    tenant_id = await _tenant(owner_conn, "disp-timeout")
    queue = OutboundQueue(create_async_engine(app_dsn))
    await queue.enqueue(tenant_id, _bubbles("sumiu"))
    channel = FakeChannel(fail_on={0: SendError("timed out")})
    scheduler = FakeScheduler()

    await Dispatcher(queue, channel, scheduler).deliver(tenant_id, "disp-timeout")

    assert [job[0] for job in scheduler.jobs] == ["deliver_outbound"]


async def test_nothing_queued_is_not_an_error(app_dsn: str, owner_conn: AsyncConnection) -> None:
    tenant_id = await _tenant(owner_conn, "disp-empty")
    queue = OutboundQueue(create_async_engine(app_dsn))
    assert await Dispatcher(queue, FakeChannel()).deliver(tenant_id, "disp-empty") == 0


async def test_a_later_reply_does_not_overtake_a_stalled_one(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """This is what the dispatcher's own stop is for.

    The queue only orders bubbles within a reply. Two replies to the same user
    are separate groups, so once the first stalls on a backoff the second is
    perfectly eligible -- and delivering it first means the user reads the
    answer to their second message before the answer to their first. The
    dispatcher stops at the first failure rather than moving on.
    """
    tenant_id = await _tenant(owner_conn, "disp-overtake")
    queue = OutboundQueue(create_async_engine(app_dsn))
    await queue.enqueue(tenant_id, _bubbles("resposta um"))
    await queue.enqueue(tenant_id, _bubbles("resposta dois"))
    channel = FakeChannel(fail_on={0: SendError("rate limited", code="130429", status=400)})

    sent = await Dispatcher(queue, channel, FakeScheduler()).deliver(tenant_id, "disp-overtake")

    assert sent == 0
    assert channel.sent == [], "the second reply jumped the queue"
