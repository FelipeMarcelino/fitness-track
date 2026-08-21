"""Drains one tenant's outbound queue through a channel (§18.5).

Per tenant rather than global, for the same two reasons the queue itself is:
RLS is FORCEd per tenant (§19.1), so a cross-tenant sweep sees nothing; and
ordering is per reply anyway, so the natural unit of work is a user.

A backoff therefore reschedules this tenant's own job rather than relying on a
global poller to come back around.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fittrack.channels.base import Channel, SendError
from fittrack.services.outbound import OutboundQueue, QueuedBubble
from fittrack.services.retry_policy import Action, backoff_seconds, classify

log = logging.getLogger(__name__)


class Scheduler(Protocol):
    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any: ...


class Dispatcher:
    """Sends bubbles until the tenant has nothing eligible left."""

    def __init__(
        self,
        queue: OutboundQueue,
        channel: Channel,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._queue = queue
        self._channel = channel
        self._scheduler = scheduler

    async def deliver(self, tenant_id: int, bsuid: str) -> int:
        """Returns how many bubbles went out.

        Stops at the first failure rather than moving on to the next bubble:
        the next bubble belongs to the same reply, and sending it after its
        predecessor failed is exactly the half-a-reply case §18.5 forbids.
        """
        sent = 0
        while True:
            bubble = await self._queue.claim_next(tenant_id)
            if bubble is None:
                return sent

            if not await self._send(bubble, bsuid):
                return sent
            sent += 1

    async def _send(self, bubble: QueuedBubble, bsuid: str) -> bool:
        try:
            provider_id = await self._channel.send(bsuid, bubble.kind, bubble.payload)
        except SendError as exc:
            await self._on_failure(bubble, exc, bsuid)
            return False

        await self._queue.mark_sent(bubble, provider_id)
        return True

    async def _on_failure(self, bubble: QueuedBubble, exc: SendError, bsuid: str) -> None:
        decision = classify(exc.code, status=exc.status)
        await self._queue.mark_failed(bubble, decision, str(exc), code=exc.code)

        if decision.action is not Action.RETRY or bubble.attempts >= decision.max_attempts:
            return
        if self._scheduler is None:
            # Without a scheduler the row is still correct -- it carries its
            # own next_retry_at -- but nothing will come back for it. Worth
            # saying out loud rather than looking like a successful retry.
            log.warning("bubble %s is scheduled to retry but nothing will wake it", bubble.id)
            return

        delay = backoff_seconds(decision, attempt=bubble.attempts)
        await self._scheduler.enqueue_job(
            "deliver_outbound", bubble.tenant_id, bsuid, _defer_by=delay
        )
