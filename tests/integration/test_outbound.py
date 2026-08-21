"""Ordered delivery and per-error-class retry (§13.6, §18.5).

Delivery state lives in the database rather than in the dispatcher's memory,
because the thing it has to survive is the dispatcher dying: a restart must
resume a half-sent reply from exactly where it stopped, without repeating the
prefix or dropping the suffix.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fittrack.services.outbound import Bubble, OutboundQueue
from fittrack.services.retry_policy import Action, classify

pytestmark = pytest.mark.integration


def _queue(app_dsn: str) -> OutboundQueue:
    return OutboundQueue(create_async_engine(app_dsn))


async def _tenant(conn: AsyncConnection, bsuid: str) -> int:
    row = await conn.execute(
        text("INSERT INTO tenant (bsuid, state) VALUES (:b, 'active') RETURNING id"),
        {"b": bsuid},
    )
    await conn.commit()
    return int(row.scalar_one())


def _bubbles(*texts: str) -> list[Bubble]:
    return [Bubble(kind="text", payload={"body": body}) for body in texts]


async def test_a_reply_leaves_in_order(app_dsn: str, owner_conn: AsyncConnection) -> None:
    """seq=1 must not go out before seq=0 has sent_at.

    Split bubbles read as one message. Delivered out of order they read as a
    non-sequitur followed by its own setup.
    """
    tenant_id = await _tenant(owner_conn, "out-order")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("primeiro", "segundo", "terceiro"))

    first = await queue.claim_next(tenant_id)
    assert first is not None
    assert first.payload["body"] == "primeiro"

    # The second is not eligible while the first has no sent_at.
    assert await queue.claim_next(tenant_id) is None

    await queue.mark_sent(first, "wamid.1")
    second = await queue.claim_next(tenant_id)
    assert second is not None
    assert second.payload["body"] == "segundo"


async def test_a_restart_resumes_without_resending_the_prefix(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The whole reason delivery state is persisted.

    A dispatcher that tracked position in memory would restart at bubble 0 and
    send the first half of the reply twice.
    """
    tenant_id = await _tenant(owner_conn, "out-restart")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("a", "b", "c"))

    first = await queue.claim_next(tenant_id)
    assert first is not None
    await queue.mark_sent(first, "wamid.a")

    # A brand new queue object: nothing carried over in memory.
    resumed = _queue(app_dsn)
    nxt = await resumed.claim_next(tenant_id)
    assert nxt is not None
    assert nxt.payload["body"] == "b", "the restart went back to the beginning"

    await resumed.mark_sent(nxt, "wamid.b")
    last = await resumed.claim_next(tenant_id)
    assert last is not None
    assert last.payload["body"] == "c"


async def test_a_claimed_bubble_is_not_handed_to_a_second_dispatcher(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Two dispatchers claiming the same bubble send it twice."""
    tenant_id = await _tenant(owner_conn, "out-claim")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("only"))

    assert await queue.claim_next(tenant_id) is not None
    assert await _queue(app_dsn).claim_next(tenant_id) is None


async def test_out_of_window_is_not_repeated_and_becomes_a_template(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """131047: the free-form message will never be accepted again."""
    tenant_id = await _tenant(owner_conn, "out-131047")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("fora da janela"))
    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None

    await queue.mark_failed(bubble, classify("131047"), "out of window", code="131047")

    row = await queue.get(tenant_id, bubble.id)
    assert row is not None
    assert row.attempts == 1, "a non-retryable error must not be attempted again"
    assert row.kind == "template", "it should have been converted, not just parked"
    assert row.sent_at is None
    assert row.dead_at is None, "converted, not given up on"


async def test_out_of_window_without_a_template_waits_for_the_window(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Nothing to convert to means waiting, not dying: the window reopens the
    moment the user writes again."""
    tenant_id = await _tenant(owner_conn, "out-nowindow")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, [Bubble(kind="image", payload={"id": "media-1"})])
    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None

    await queue.mark_failed(bubble, classify("131047"), "out of window", code="131047")

    row = await queue.get(tenant_id, bubble.id)
    assert row is not None
    assert row.dead_at is None
    assert row.kind == "image"
    # Parked rather than eligible: claiming again must not pick it back up.
    assert await _queue(app_dsn).claim_next(tenant_id) is None


async def test_an_undeliverable_recipient_is_dead_and_suspends_the_tenant(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """131026: retrying any message to this recipient fails identically, and
    proactive sends have to stop -- not just this bubble."""
    tenant_id = await _tenant(owner_conn, "out-131026")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("oi"))
    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None

    await queue.mark_failed(bubble, classify("131026"), "cannot receive", code="131026")

    row = await queue.get(tenant_id, bubble.id)
    assert row is not None
    assert row.dead_at is not None

    state = await owner_conn.execute(
        text("SELECT state FROM tenant WHERE id = :i"), {"i": tenant_id}
    )
    assert state.scalar_one() == "suspended"


async def test_a_rate_limit_is_rescheduled_with_backoff(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """130429 is the one class where repetition genuinely helps."""
    tenant_id = await _tenant(owner_conn, "out-130429")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("depois"))
    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None

    await queue.mark_failed(bubble, classify("130429"), "rate limited", code="130429")

    row = await queue.get(tenant_id, bubble.id)
    assert row is not None
    assert row.dead_at is None
    assert row.error_code == "130429"
    assert row.retryable is True
    # Not eligible yet: the backoff has to actually hold it back.
    assert await _queue(app_dsn).claim_next(tenant_id) is None


async def test_a_rate_limit_dies_once_the_attempts_run_out(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Retrying forever is its own outage."""
    tenant_id = await _tenant(owner_conn, "out-exhaust")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("teimoso"))
    decision = classify("130429")

    for _ in range(decision.max_attempts):
        bubble = await queue.claim_next(tenant_id, now_offset=3600)
        assert bubble is not None
        await queue.mark_failed(bubble, decision, "rate limited", code="130429")

    row = await queue.get(tenant_id, bubble.id)
    assert row is not None
    assert row.dead_at is not None, "it retried past its budget"
    assert await queue.claim_next(tenant_id, now_offset=3600) is None


async def test_a_malformed_payload_dies_immediately(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """100 is our bug. The identical payload gets the identical rejection."""
    tenant_id = await _tenant(owner_conn, "out-100")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("payload ruim"))
    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None

    await queue.mark_failed(bubble, classify("100"), "invalid parameter", code="100")

    row = await queue.get(tenant_id, bubble.id)
    assert row is not None
    assert row.dead_at is not None
    assert row.attempts == 1


async def test_a_dead_bubble_kills_the_rest_of_its_reply(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Half a reply is worse than none.

    Bubble 2 alone reads as a fragment answering a question the user never saw
    asked, and there is no way for them to tell that something is missing.
    """
    tenant_id = await _tenant(owner_conn, "out-cascade")
    queue = _queue(app_dsn)
    group = await queue.enqueue(tenant_id, _bubbles("um", "dois", "tres"))

    first = await queue.claim_next(tenant_id)
    assert first is not None
    await queue.mark_failed(first, classify("100"), "invalid parameter", code="100")

    rows = await queue.group(tenant_id, group)
    assert len(rows) == 3
    assert all(row.dead_at is not None for row in rows), (
        "the rest of the reply is still queued to go out on its own"
    )
    assert await _queue(app_dsn).claim_next(tenant_id) is None


async def test_a_dead_bubble_does_not_touch_another_reply(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The cascade is scoped to the group, not the tenant."""
    tenant_id = await _tenant(owner_conn, "out-scope")
    queue = _queue(app_dsn)
    doomed = await queue.enqueue(tenant_id, _bubbles("ruim"))
    other = await queue.enqueue(tenant_id, _bubbles("boa"))

    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None
    await queue.mark_failed(bubble, classify("100"), "invalid parameter", code="100")

    assert all(row.dead_at is not None for row in await queue.group(tenant_id, doomed))
    assert all(row.dead_at is None for row in await queue.group(tenant_id, other))


async def test_an_account_block_alerts_without_retrying(
    app_dsn: str, owner_conn: AsyncConnection
) -> None:
    tenant_id = await _tenant(owner_conn, "out-368")
    queue = _queue(app_dsn)
    await queue.enqueue(tenant_id, _bubbles("bloqueado"))
    bubble = await queue.claim_next(tenant_id)
    assert bubble is not None

    decision = classify("368")
    assert decision.action is Action.ALERT
    await queue.mark_failed(bubble, decision, "account restricted", code="368")

    row = await queue.get(tenant_id, bubble.id)
    assert row is not None
    assert row.dead_at is not None
