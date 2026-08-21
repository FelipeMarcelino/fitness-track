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
    await buffer.push("B1", {"message_id": "m1", "text": "supino 80x8"})

    batch = await buffer.drain("B1", batch_id="b1")

    assert [m["message_id"] for m in batch] == ["m1"]


async def test_a_burst_becomes_a_single_batch(buffer: BurstBuffer) -> None:
    """The case from §4: four fragments typed between sets."""
    for i, text in enumerate(["supino reto", "10kg", "8 reps", "foi facil"]):
        await buffer.push("B2", {"message_id": f"m{i}", "text": text})

    batch = await buffer.drain("B2", batch_id="b2")

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

    batch = await buffer.drain("B3", batch_id="b3")
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
            """ingress writing while the worker drains."""
            if not self._injected:
                self._injected = True
                await self._inner.rpush(buffer_key, json.dumps({"message_id": "late"}))

        async def rename(self, source: str, target: str) -> object:
            """The correct path: the write lands after an atomic move, so it
            goes into a fresh buffer and is picked up by the next drain."""
            result = await self._inner.rename(source, target)
            await self._inject(source)
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
    await buffer.push("B5", {"message_id": "early"})

    first = await buffer.drain("B5", batch_id="b5")
    second = await buffer.drain("B5", batch_id="b5b")

    delivered = [m["message_id"] for m in first + second]
    assert "early" in delivered
    assert "late" in delivered, "a message written during the drain was lost"


async def test_draining_an_empty_buffer_yields_nothing(buffer: BurstBuffer) -> None:
    """A flush job that fires after another worker already drained must not
    create an empty batch, which would cost an LLM call to produce nothing."""
    assert await buffer.drain("B6", batch_id="b6") == []


async def test_orphan_drain_keys_are_reclaimed(buffer: BurstBuffer, client: aioredis.Redis) -> None:
    """A worker that dies between RENAME and DEL leaves the batch in a drain
    key. Without reclamation those messages are stranded: they are out of the
    buffer, so no flush will ever pick them up again."""
    await buffer.push("B7", {"message_id": "m1"})
    await client.rename(buffer.buffer_key("B7"), buffer.drain_key("B7", "dead"))

    reclaimed = await buffer.reclaim_orphans()

    assert reclaimed == 1
    batch = await buffer.drain("B7", batch_id="b7")
    assert [m["message_id"] for m in batch] == ["m1"]


async def test_buffers_of_different_users_do_not_mix(buffer: BurstBuffer) -> None:
    await buffer.push("A", {"message_id": "a1"})
    await buffer.push("B", {"message_id": "b1"})

    assert [m["message_id"] for m in await buffer.drain("A", batch_id="x")] == ["a1"]
    assert [m["message_id"] for m in await buffer.drain("B", batch_id="y")] == ["b1"]
