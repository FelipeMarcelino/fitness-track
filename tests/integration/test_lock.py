"""Per-user serialisation (§17.3).

Two bursts from the same person must not run at once: set 2 landing before
set 1 would record them out of order, and set_index is what the analytics read.
Bursts from different people must run at once, or one slow LLM call would
serialise the whole system.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from testcontainers.redis import RedisContainer

from fittrack.services.lock import LockLostError, UserLock

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def client(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    conn = aioredis.from_url(redis_url, decode_responses=True)
    await conn.flushdb()
    yield conn
    await conn.aclose()


async def test_the_same_user_serialises(client: aioredis.Redis) -> None:
    """Set 2 recorded before set 1 puts the workout out of order, and
    set_index is what every progression query reads."""
    order: list[str] = []

    async def work(tag: str) -> None:
        async with UserLock(client, "SAME") as lock:
            if not lock.acquired:
                order.append(f"{tag}-refused")
                return
            order.append(f"{tag}-start")
            await asyncio.sleep(0.15)
            order.append(f"{tag}-end")

    await asyncio.gather(work("a"), work("b"))

    # One got in and finished; the other was refused rather than interleaved.
    starts = [o for o in order if o.endswith("-start")]
    refused = [o for o in order if o.endswith("-refused")]
    assert len(starts) == 1, f"two holders at once: {order}"
    assert len(refused) == 1, f"the contender was not refused: {order}"

    # The refusal lands between the holder's start and end -- the contender is
    # turned away while the holder is still working, which is the point. What
    # must never appear is a second start.
    winner = starts[0].removesuffix("-start")
    assert order.index(f"{winner}-end") > order.index(f"{winner}-start"), order


async def test_different_users_run_at_the_same_time(
    client: aioredis.Redis,
) -> None:
    """Serialising across users would make one slow LLM call block everybody."""
    started = asyncio.Event()

    async def first() -> None:
        async with UserLock(client, "USER-A") as lock:
            assert lock.acquired
            started.set()
            await asyncio.sleep(0.2)

    async def second() -> bool:
        await started.wait()
        async with UserLock(client, "USER-B") as lock:
            return lock.acquired

    _, acquired = await asyncio.gather(first(), second())

    assert acquired, "a different user was blocked"


async def test_the_lock_is_released_when_the_body_raises(
    client: aioredis.Redis,
) -> None:
    """A crash mid-batch must not lock the user out until the TTL expires."""

    async def failing_work() -> None:
        async with UserLock(client, "BOOM") as lock:
            assert lock.acquired
            raise RuntimeError("processing failed")

    with pytest.raises(RuntimeError):
        await failing_work()

    async with UserLock(client, "BOOM") as lock:
        assert lock.acquired, "the lock survived the exception"


async def test_a_stale_lock_expires(client: aioredis.Redis) -> None:
    """A worker killed without unwinding leaves the key behind. Without a TTL
    that user would never be processed again."""
    lock = UserLock(client, "STALE", ttl_seconds=1)
    assert await lock.acquire()

    ttl = await client.ttl(lock.key)
    assert 0 < ttl <= 1


async def test_only_the_owner_releases(client: aioredis.Redis) -> None:
    """Delete-by-key would let a worker whose lock already expired delete the
    lock a second worker legitimately holds, and then both run at once."""
    first = UserLock(client, "OWNER")
    assert await first.acquire()
    await client.delete(first.key)  # simulate expiry

    second = UserLock(client, "OWNER")
    assert await second.acquire()

    await first.release()  # the original owner, now stale

    assert await client.exists(second.key), "a stale holder released someone else's lock"


async def test_the_lock_is_extended_while_work_continues(
    client: aioredis.Redis,
) -> None:
    """An analysis can outlive the TTL. Without renewal the lock disappears
    mid-batch and a second worker starts on the same user."""
    # The sleep has to outlast the TTL, or the key survives with or without
    # renewal and the test proves nothing -- an earlier version slept 1.2s
    # against a 2s TTL and passed with renewal removed entirely.
    lock = UserLock(client, "LONG", ttl_seconds=2, renew_every=0.4)
    async with lock as held:
        assert held.acquired
        await asyncio.sleep(3.0)
        ttl = await client.ttl(lock.key)
        still_held = await client.get(lock.key)

    assert still_held is not None, "the lock expired while work was still running"
    assert ttl > 0


async def test_work_is_abandoned_when_the_lock_is_lost(client: aioredis.Redis) -> None:
    """Noticing the loss is not enough; the work has to stop.

    Renewal can fail -- a Redis failover, a paused process, a key evicted --
    and by then another worker may already be writing this user's sets. A
    holder that logs the loss and keeps going produces exactly the concurrent
    write the lock exists to prevent.
    """
    finished = False

    async def slow_work() -> str:
        nonlocal finished
        await asyncio.sleep(5.0)
        finished = True
        return "done"

    lock = UserLock(client, "LOSE-ME", ttl_seconds=2, renew_every=0.2)
    async with lock:
        assert lock.acquired
        # Someone else takes the key: renewal will find a token that is not ours.
        await client.set(lock.key, "another-worker")

        with pytest.raises(LockLostError):
            await lock.guard(slow_work())

    # Give a leaked task a chance to finish, if cancellation did not land.
    await asyncio.sleep(0.3)
    assert not finished, "the work carried on after the lock was gone"

    # And the stolen lock is still the other worker's: release must not take it.
    assert await client.get(lock.key) == "another-worker"


async def test_work_that_finishes_in_time_is_not_disturbed(client: aioredis.Redis) -> None:
    """The guard must be invisible when nothing goes wrong."""
    async with UserLock(client, "KEEP-ME", ttl_seconds=5, renew_every=0.2) as lock:
        assert await lock.guard(asyncio.sleep(0.3, result="ok")) == "ok"
