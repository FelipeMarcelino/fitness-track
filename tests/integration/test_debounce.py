"""Burst buffering and the renewable debounce window (§17.1, §17.3).

Tests use a fake clock rather than sleeping: a suite that waits out a ten
second window takes a minute to tell you anything, and nobody runs it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from testcontainers.redis import RedisContainer

from fittrack.services.debounce import BurstBuffer

pytestmark = pytest.mark.integration

WINDOW = 10


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


@pytest_asyncio.fixture
async def buffer(client: aioredis.Redis) -> BurstBuffer:
    return BurstBuffer(client, window_seconds=WINDOW)


async def test_one_message_becomes_one_batch(buffer: BurstBuffer) -> None:
    await buffer.push("B1", {"message_id": "m1", "text": "supino 80x8"}, now=0.0)

    batch = await buffer.drain("B1", batch_id="b1", now=WINDOW + 1)

    assert [m["message_id"] for m in batch] == ["m1"]


async def test_a_burst_becomes_a_single_batch(buffer: BurstBuffer) -> None:
    """The case from §4: four fragments typed between sets."""
    for i, text in enumerate(["supino reto", "10kg", "8 reps", "foi facil"]):
        await buffer.push("B2", {"message_id": f"m{i}", "text": text}, now=i * 2.0)

    batch = await buffer.drain("B2", batch_id="b2", now=6.0 + WINDOW + 1)

    assert [m["text"] for m in batch] == ["supino reto", "10kg", "8 reps", "foi facil"]


async def test_the_window_renews_on_every_message(buffer: BurstBuffer) -> None:
    """Six messages six seconds apart span 36 seconds with every gap under the
    ten second window.

    An implementation that schedules a single flush ten seconds after the
    *first* message passes the burst test above and splits this one into four
    batches -- which in production means a long burst gets chopped mid-exercise.
    """
    now = 1_000.0
    for i in range(6):
        await buffer.push("B3", {"message_id": f"m{i}"}, now=now + i * 6)

    # 35s in, one second before the last message's window closes
    assert not await buffer.is_ready("B3", now=now + 35)
    # 36 + 10: the window closed on the last message, not the first
    assert await buffer.is_ready("B3", now=now + 46)

    batch = await buffer.drain("B3", batch_id="b3", now=now + 46)
    assert len(batch) == 6


async def test_flush_waits_for_silence_not_for_the_first_message(
    buffer: BurstBuffer,
) -> None:
    await buffer.push("B4", {"message_id": "m1"}, now=100.0)
    await buffer.push("B4", {"message_id": "m2"}, now=105.0)

    assert not await buffer.is_ready("B4", now=111.0)  # 11s after the first
    assert await buffer.is_ready("B4", now=116.0)  # 11s after the last


async def test_a_message_arriving_during_the_drain_is_not_lost(
    client: aioredis.Redis,
) -> None:
    """The regression this design exists for (§17.3).

    ingress writes to the buffer without holding the worker's lock -- it has to
    answer Meta in under 200 ms -- so a message can land in the middle of a
    drain. LRANGE followed by DEL reads the list, and the DEL then removes the
    message that arrived in between: silent data loss, no error, no log, just a
    set the user reported and the bot never recorded.

    The test forces that interleaving by pushing from inside the read call, so
    it fails against LRANGE+DEL and passes against RENAME. Without the
    injection the race never happens and the test proves nothing -- an earlier
    version did exactly that and passed against the broken implementation.
    """

    class InterleavingRedis:
        """Delegates to Redis, but slips a concurrent push into the read."""

        def __init__(self, inner: aioredis.Redis) -> None:
            self._inner = inner
            self._injected = False

        async def _inject(self, buffer_key: str) -> None:
            """ingress writing while the worker drains.

            A real push, deadline included: a message whose deadline is missing
            is never considered ready, so injecting the list entry alone would
            make the assertion fail for the wrong reason.
            """
            if self._injected:
                return
            self._injected = True
            bsuid = buffer_key.removeprefix("buffer:")
            await self._inner.rpush(buffer_key, json.dumps({"message_id": "late"}))
            await self._inner.set(f"deadline:{bsuid}", 0)

        async def eval(self, script: str, numkeys: int, *args: object) -> object:
            """The correct path: the claim is one atomic script, so the write
            lands entirely before or entirely after it. Either way it survives
            -- included in this batch, or waiting in a fresh buffer."""
            result = await self._inner.eval(script, numkeys, *args)
            await self._inject(str(args[0]))
            return result

        async def lrange(self, key: str, start: int, end: int) -> list[str]:
            """The broken path: the write lands between the read and the
            delete, and the delete takes it with it."""
            result = await self._inner.lrange(key, start, end)
            if key.startswith("buffer:"):
                await self._inject(key)
            return result

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    buffer = BurstBuffer(InterleavingRedis(client), window_seconds=WINDOW)  # type: ignore[arg-type]
    await buffer.push("B5", {"message_id": "early"}, now=0.0)

    first = await buffer.drain("B5", batch_id="b5", now=WINDOW + 1)
    second = await buffer.drain("B5", batch_id="b5b", now=2 * WINDOW + 3)

    delivered = [m["message_id"] for m in first + second]
    assert "early" in delivered
    assert "late" in delivered, "a message written during the drain was lost"


async def test_draining_an_empty_buffer_yields_nothing(buffer: BurstBuffer) -> None:
    """A flush job that fires after another worker already drained must not
    create an empty batch, which would cost an LLM call to produce nothing."""
    assert await buffer.drain("B6", batch_id="b6", now=WINDOW + 1) == []


async def test_orphan_drain_keys_are_reclaimed(buffer: BurstBuffer, client: aioredis.Redis) -> None:
    """A worker that dies between RENAME and DEL leaves the batch in a drain
    key. Without reclamation those messages are stranded: they are out of the
    buffer, so no flush will ever pick them up again."""
    await buffer.push("B7", {"message_id": "m1"}, now=0.0)
    await client.rename(buffer.buffer_key("B7"), buffer.drain_key("B7", "dead"))

    reclaimed = await buffer.reclaim_orphans()

    assert reclaimed == 1
    batch = await buffer.drain("B7", batch_id="b7", now=WINDOW + 1)
    assert [m["message_id"] for m in batch] == ["m1"]


async def test_buffers_of_different_users_do_not_mix(buffer: BurstBuffer) -> None:
    await buffer.push("A", {"message_id": "a1"}, now=0.0)
    await buffer.push("B", {"message_id": "b1"}, now=0.0)
    later = WINDOW + 1

    assert [m["message_id"] for m in await buffer.drain("A", "x", now=later)] == ["a1"]
    assert [m["message_id"] for m in await buffer.drain("B", "y", now=later)] == ["b1"]


async def test_a_push_between_claim_and_cleanup_keeps_its_deadline(
    buffer: BurstBuffer, client: aioredis.Redis
) -> None:
    """Claiming used to be three commands: check readiness, rename, delete the
    deadline. A push landing between the rename and the delete had its
    brand-new deadline deleted, and the message then sat in the buffer with
    is_ready false forever -- drained only if some later message happened to
    arrive. The claim is one script now, so nothing can land in the middle.
    """
    await buffer.push("B8", {"message_id": "first"}, now=100.0)

    assert await buffer.drain("B8", batch_id="b8", now=120.0)

    await buffer.push("B8", {"message_id": "second"}, now=121.0)
    assert await buffer.is_ready("B8", now=132.0), "the new deadline was destroyed"
    assert [m["message_id"] for m in await buffer.drain("B8", "b8b", now=132.0)] == ["second"]


async def test_a_batch_is_not_claimed_before_its_window_closes(
    buffer: BurstBuffer,
) -> None:
    """Readiness and the claim are checked together. Apart, a message arriving
    between the two is swept into a batch whose own silence window has not
    elapsed: the first fragment of the next burst gets processed alone and the
    rest arrive as a second batch."""
    await buffer.push("B9", {"message_id": "m1"}, now=200.0)

    assert await buffer.drain("B9", batch_id="early", now=205.0) == []
    assert len(await buffer.drain("B9", batch_id="ontime", now=211.0)) == 1


async def test_reclaim_leaves_a_live_worker_alone(
    buffer: BurstBuffer, client: aioredis.Redis
) -> None:
    """Maintenance runs while workers work. Without a lease the scan reclaims a
    batch that is being processed right now, and the same sets are recorded
    twice -- which for a workout log means doubled volume."""
    await buffer.push("B10", {"message_id": "m1"}, now=300.0)

    # Claimed but not yet collected: exactly the state a worker is in while it
    # processes a batch, and the state reclaim_orphans must leave alone.
    assert await buffer.claim("B10", batch_id="live", now=311.0)

    assert await buffer.reclaim_orphans() == 0, "reclaimed a batch in flight"
    assert len(await buffer.collect("B10", batch_id="live")) == 1
