"""Burst buffering with a renewable silence window (§17.1, §17.3).

People type a set in fragments -- "supino reto", "10kg", "8 reps", "foi facil"
-- between rounds. Processing each fragment separately costs four LLM calls,
produces four half-formed records, and lets the bot ask about something the
next message already answered. So messages accumulate and are handed over as
one batch once the user has been quiet for a while.

Two decisions here are load-bearing and neither is obvious.

The window renews on every message rather than being scheduled once from the
first. Scheduling once looks equivalent on a four-message burst and splits a
long one mid-exercise.

Draining uses RENAME rather than reading and deleting. ingress writes to the
buffer without holding the worker's lock -- it has to answer Meta in under
200 ms -- so a message can land between the read and the delete, and would be
deleted without ever being processed. That is silent data loss: no error, no
log, just a set the user reported and the bot never recorded.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Final

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

BUFFER_PREFIX: Final = "buffer:"
DRAIN_PREFIX: Final = "drain:"
DEADLINE_PREFIX: Final = "deadline:"
LEASE_PREFIX: Final = "drainlease:"

# How long a worker is assumed to still be working on a claimed batch.
# Longer than the slowest realistic batch, shorter than a shift.
LEASE_SECONDS: Final = 300

# Readiness and the claim must happen together. Checked separately, a
# message arriving in between is swept into a batch whose own silence
# window has not elapsed -- the first fragment of the next burst gets
# processed alone, and the rest arrive as a second batch. Redis runs a
# script atomically, so nothing can interleave.
CLAIM_SCRIPT: Final = """
local deadline = redis.call('GET', KEYS[2])
if not deadline then return 0 end
if tonumber(ARGV[1]) < tonumber(deadline) then return 0 end
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('RENAME', KEYS[1], KEYS[3])
redis.call('DEL', KEYS[2])
redis.call('SET', KEYS[4], 1, 'EX', ARGV[2])
return 1
"""

# Long enough that the buffer survives a worker restart, short enough that a
# forgotten key does not live forever.
BUFFER_TTL_SECONDS: Final = 3600


class BurstBuffer:
    """Redis-backed buffer for one user's in-flight messages."""

    def __init__(self, client: aioredis.Redis, window_seconds: int = 10) -> None:
        self._redis = client
        self._window = window_seconds

    def buffer_key(self, bsuid: str) -> str:
        return f"{BUFFER_PREFIX}{bsuid}"

    def drain_key(self, bsuid: str, batch_id: str) -> str:
        return f"{DRAIN_PREFIX}{bsuid}:{batch_id}"

    def _deadline_key(self, bsuid: str) -> str:
        return f"{DEADLINE_PREFIX}{bsuid}"

    def _lease_key(self, bsuid: str, batch_id: str) -> str:
        return f"{LEASE_PREFIX}{bsuid}:{batch_id}"

    async def push(self, bsuid: str, message: dict[str, Any], now: float | None = None) -> None:
        """Appends a message and pushes the silence deadline forward.

        The deadline is stored as a value rather than relied on as a key TTL,
        because `is_ready` needs to answer "how long since the last message"
        and an expiring key can only answer "has it expired".
        """
        moment = time.time() if now is None else now

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(self.buffer_key(bsuid), json.dumps(message))
            pipe.expire(self.buffer_key(bsuid), BUFFER_TTL_SECONDS)
            pipe.set(
                self._deadline_key(bsuid),
                moment + self._window,
                ex=BUFFER_TTL_SECONDS,
            )
            await pipe.execute()

    async def is_ready(self, bsuid: str, now: float | None = None) -> bool:
        """True once the user has been silent for the whole window."""
        raw = await self._redis.get(self._deadline_key(bsuid))
        if raw is None:
            return False
        moment = time.time() if now is None else now
        return moment >= float(raw)

    async def drain(
        self, bsuid: str, batch_id: str, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Atomically claims the buffer and returns its contents.

        RENAME is the whole point: it moves the list in one operation, so a
        message written a microsecond later lands in a new, empty buffer
        instead of being caught by a delete. Never LRANGE followed by DEL.

        The rename, the readiness check and the deadline delete all run inside
        one script. Done as separate commands, a push landing between the
        rename and the delete has its brand-new deadline deleted, and the
        message then sits in the buffer with `is_ready` false forever -- it
        would only be drained if some later message happened to arrive.
        """
        if not await self.claim(bsuid, batch_id, now=now):
            return []
        return await self.collect(bsuid, batch_id)

    async def claim(self, bsuid: str, batch_id: str, now: float | None = None) -> bool:
        """Takes ownership of the buffer if the silence window has closed.

        Separate from `collect` because the gap between them is where a worker
        can die, and that gap is exactly what `reclaim_orphans` looks for. The
        lease this sets is how maintenance tells a dead worker from a busy one.
        """
        moment = time.time() if now is None else now
        claimed = await self._redis.eval(  # type: ignore[misc]
            CLAIM_SCRIPT,
            4,
            self.buffer_key(bsuid),
            self._deadline_key(bsuid),
            self.drain_key(bsuid, batch_id),
            self._lease_key(bsuid, batch_id),
            # Redis takes script arguments as strings; Lua converts back
            # with tonumber().
            str(moment),
            str(LEASE_SECONDS),
        )
        # Zero means not silent yet, already claimed by another worker, or
        # nothing arrived. All three mean the same thing here: nothing to do.
        return bool(int(claimed))

    async def collect(self, bsuid: str, batch_id: str) -> list[dict[str, Any]]:
        """Reads and clears an already-claimed drain key.

        Separate from `drain` so a worker that crashed after the RENAME can be
        recovered: the batch is still sitting in its drain key.
        """
        key = self.drain_key(bsuid, batch_id)
        # redis-py types lrange as sync-or-async on the shared stub; the
        # async client always returns an awaitable.
        raw: list[str] = await self._redis.lrange(key, 0, -1)  # type: ignore[misc]
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.delete(self._lease_key(bsuid, batch_id))
            await pipe.execute()
        return [json.loads(item) for item in raw]

    async def reclaim_orphans(self) -> int:
        """Returns stranded batches to their buffers.

        A worker that dies between RENAME and DEL leaves messages in a drain
        key. They are out of the buffer, so no flush will ever look at them
        again -- without this they are lost as surely as if they had been
        deleted. Run from the maintenance queue (§17.2).
        """
        reclaimed = 0
        async for key in self._redis.scan_iter(match=f"{DRAIN_PREFIX}*"):
            bsuid, _, batch_id = key[len(DRAIN_PREFIX) :].rpartition(":")

            # A live worker holds a lease on its drain key. Without this check
            # the scan reclaims a batch that is being processed right now, and
            # the same sets are recorded twice.
            if await self._redis.exists(self._lease_key(bsuid, batch_id)):
                continue
            items: list[str] = await self._redis.lrange(key, 0, -1)  # type: ignore[misc]
            if not items:
                await self._redis.delete(key)
                continue

            # Prepend: these arrived before whatever is in the buffer now.
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.lpush(self.buffer_key(bsuid), *reversed(items))
                pipe.expire(self.buffer_key(bsuid), BUFFER_TTL_SECONDS)
                # Deadline in the past: a reclaimed batch is already late,
                # so it should go out on the next flush rather than wait
                # out another silence window.
                pipe.set(self._deadline_key(bsuid), 0, ex=BUFFER_TTL_SECONDS)
                pipe.delete(key)
                await pipe.execute()

            log.warning("reclaimed %d orphaned messages for %s", len(items), bsuid)
            reclaimed += 1
        return reclaimed
