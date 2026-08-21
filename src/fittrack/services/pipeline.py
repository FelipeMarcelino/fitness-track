"""The path from a drained burst to a processed batch (§4, §17).

Sprint 01 stops at the batch: the graph arrives in feat/echo-graph. What is
here is the part that has to be right before any agent exists -- ordering,
durability and what happens when something fails.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Final
from uuid import uuid4

import redis.asyncio as aioredis

from fittrack.services.batch import MAX_ATTEMPTS, Batch, BatchStore
from fittrack.services.debounce import BurstBuffer
from fittrack.services.lock import UserLock

log = logging.getLogger(__name__)

# How long to wait before another worker retries a user whose lock is held.
# Short: the holder is usually mid-LLM-call and will be done shortly.
BUSY_RETRY_SECONDS: Final = 5

Handler = Callable[[Batch], Awaitable[None]]


class Pipeline:
    """Drains one user's burst and hands it to the handler, exactly once."""

    def __init__(
        self,
        redis: aioredis.Redis,
        buffer: BurstBuffer,
        batches: BatchStore,
        handler: Handler,
    ) -> None:
        self._redis = redis
        self._buffer = buffer
        self._batches = batches
        self._handler = handler

    async def flush(self, bsuid: str) -> Batch | None:
        """Returns the batch that was processed, or None if there was nothing
        to do or someone else is doing it.

        Returning None for "someone else holds the lock" is deliberate: the
        caller reschedules rather than waiting. Waiting would tie up a worker
        slot for the length of another user's LLM call, and under load that
        turns into every worker blocked on a handful of users.
        """
        async with UserLock(self._redis, bsuid) as lock:
            if not lock.acquired:
                log.debug("%s is already being processed; will retry", bsuid)
                return None

            batch_id = uuid4().hex
            messages = await self._buffer.drain(bsuid, batch_id=batch_id)
            if not messages:
                return None

            tenant_id = int(messages[0]["tenant_id"])
            batch = await self._batches.create(tenant_id, messages)
            if batch is None:
                return None

            await self._run(batch)
            return batch

    async def _run(self, batch: Batch) -> None:
        """Runs the handler, counting the attempt before rather than after.

        Counting afterwards means a handler that crashes the process never
        records its attempt, and the batch retries forever.
        """
        attempts = await self._batches.mark_attempt(batch)
        try:
            await self._handler(batch)
        except Exception as exc:
            if attempts >= MAX_ATTEMPTS:
                log.exception("batch %s exhausted its retries", batch.id)
                await self._batches.mark_failed(batch, str(exc))
                return
            log.warning("batch %s failed on attempt %d: %s", batch.id, attempts, exc)
            raise
        else:
            await self._batches.mark_done(batch)
