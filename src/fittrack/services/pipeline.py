"""The path from a drained burst to a processed batch (§4, §17).

Sprint 01 stops at the batch: the graph arrives in feat/echo-graph. What is
here is the part that has to be right before any agent exists -- ordering,
durability and what happens when something fails.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Final
from uuid import uuid4

import redis.asyncio as aioredis

from fittrack.services.batch import MAX_ATTEMPTS, Batch, BatchStore
from fittrack.services.debounce import BurstBuffer
from fittrack.services.lock import LockLostError, UserLock

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
            if not await self._buffer.claim(bsuid, batch_id=batch_id):
                # Nothing buffered, or the window has not closed. A previous
                # attempt may still owe work, though: the drain took its
                # messages, so only the database knows about it.
                return await self._resume(bsuid, lock)

            # Read without dropping. Deleting here and persisting afterwards
            # leaves a window where a dying worker loses the burst from both
            # Redis and Postgres, with nothing for the reclaimer to find.
            messages = await self._buffer.peek(bsuid, batch_id)
            if not messages:
                await self._buffer.discard(bsuid, batch_id)
                return None

            tenant_id = int(messages[0]["tenant_id"])
            batch = await self._batches.create(tenant_id, messages)
            if batch is None:
                await self._buffer.discard(bsuid, batch_id)
                return None

            # Durable now; Redis can let go.
            await self._buffer.discard(bsuid, batch_id)

            return await self._run(batch, lock)

    async def _resume(self, bsuid: str, lock: UserLock) -> Batch | None:
        """Picks up a batch a previous attempt left pending.

        The retry job carries only the user id, and Redis no longer holds the
        messages. Without this the failed batch is never touched again."""
        tenant_id = await self._tenant_of(bsuid)
        if tenant_id is None:
            return None

        batch = await self._batches.pending_for(tenant_id)
        if batch is None:
            return None

        log.info("resuming batch %s after a previous failure", batch.id)
        return await self._run(batch, lock)

    async def _tenant_of(self, bsuid: str) -> int | None:
        return await self._batches.tenant_id_for(bsuid)

    async def _run(self, batch: Batch, lock: UserLock) -> Batch:
        """Runs the handler, counting the attempt before rather than after.

        Counting afterwards means a handler that crashes the process never
        records its attempt, and the batch retries forever.
        """
        attempts = await self._batches.mark_attempt(batch)
        # Batch is frozen, so the counter is carried forward in a new object
        # rather than mutated. `exhausted` reads this field, and returning the
        # pre-increment value would understate how close the batch is to being
        # given up on.
        batch = replace(batch, attempts=attempts)
        try:
            # Under the lock's guard: if the lease is lost mid-handler another
            # worker is already on this user, and continuing would write the
            # same sets twice.
            await lock.guard(self._handler(batch))
        except LockLostError:
            # Not the batch's fault, so it is left pending rather than being
            # marked failed. The row keeps its incremented attempt, which is
            # the honest count of how many times it has been picked up.
            log.warning("gave up batch %s: the lock was lost mid-flight", batch.id)
            raise
        except Exception as exc:
            if attempts >= MAX_ATTEMPTS:
                log.exception("batch %s exhausted its retries", batch.id)
                await self._batches.mark_failed(batch, str(exc))
                return batch
            log.warning("batch %s failed on attempt %d: %s", batch.id, attempts, exc)
            raise
        else:
            await self._batches.mark_done(batch)
            return batch
