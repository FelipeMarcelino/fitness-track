"""Per-user processing lock (§17.3).

Bursts from the same person are serialised so sets are recorded in the order
they were sent; set_index is what every progression query reads, and a set 2
written before set 1 is wrong in a way nothing downstream can detect. Bursts
from different people run concurrently, because serialising across users would
let one slow LLM call block everybody.

The lock guards workers against each other only. ingress never takes it: it has
to answer Meta in under 200 ms, and waiting on a lock held by an LLM call would
blow that budget. That is why draining the buffer has to be atomic on its own
(§17.3) rather than relying on this.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import TracebackType
from typing import Final, Self

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

LOCK_PREFIX: Final = "lock:"
DEFAULT_TTL_SECONDS: Final = 120
DEFAULT_RENEW_EVERY: Final = 30.0

# Releasing compares the token before deleting. A plain DEL would let a worker
# whose lock already expired delete the lock a second worker legitimately
# holds, and then both process the same user at once -- which is exactly the
# interleaving this lock exists to prevent.
RELEASE_SCRIPT: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

RENEW_SCRIPT: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class UserLock:
    """Async context manager. Check `.acquired` before doing the work.

    Non-blocking by design: a worker that cannot get the lock returns the batch
    to the queue rather than waiting. Waiting would tie up a worker slot for
    the length of someone else's LLM call.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        bsuid: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        renew_every: float = DEFAULT_RENEW_EVERY,
    ) -> None:
        self._redis = client
        self.key = f"{LOCK_PREFIX}{bsuid}"
        self._ttl = ttl_seconds
        self._renew_every = renew_every
        # Identifies this holder, so release and renew cannot touch anyone
        # else's lock.
        self._token = uuid.uuid4().hex
        self.acquired = False
        self._renewal: asyncio.Task[None] | None = None

    async def acquire(self) -> bool:
        self.acquired = bool(await self._redis.set(self.key, self._token, nx=True, ex=self._ttl))
        return self.acquired

    async def release(self) -> None:
        if self._renewal is not None:
            self._renewal.cancel()
            self._renewal = None
        await self._redis.eval(RELEASE_SCRIPT, 1, self.key, self._token)  # type: ignore[misc]
        self.acquired = False

    async def __aenter__(self) -> Self:
        if await self.acquire():
            self._renewal = asyncio.create_task(self._keep_alive())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Released even when the body raised: otherwise a crash mid-batch locks
        # the user out until the TTL runs down.
        if self.acquired:
            await self.release()

    async def _keep_alive(self) -> None:
        """Extends the lock while the work is still running.

        An analysis can outlive the TTL. Without renewal the key expires
        mid-batch, a second worker picks the same user up, and the two write
        sets concurrently -- the failure this lock exists to prevent, arriving
        by way of the lock itself.
        """
        try:
            while True:
                await asyncio.sleep(self._renew_every)
                extended = await self._redis.eval(  # type: ignore[misc]
                    RENEW_SCRIPT, 1, self.key, self._token, str(self._ttl)
                )
                if not int(extended):
                    # Someone else holds it now; stop pretending we do.
                    log.warning("lost the lock on %s while working", self.key)
                    self.acquired = False
                    return
        except asyncio.CancelledError:
            return
