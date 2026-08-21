"""End to end from buffer to batch, under the lock (§4, §17)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from testcontainers.redis import RedisContainer

from fittrack.services.batch import MAX_ATTEMPTS, Batch, BatchStore
from fittrack.services.debounce import BurstBuffer
from fittrack.services.lock import UserLock
from fittrack.services.pipeline import Pipeline

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


async def _tenant(conn: AsyncConnection, bsuid: str) -> int:
    row = await conn.execute(
        text("INSERT INTO tenant (bsuid) VALUES (:b) RETURNING id"), {"b": bsuid}
    )
    await conn.commit()
    return int(row.scalar_one())


def _pipeline(
    client: aioredis.Redis, app_dsn: str, handler: object
) -> tuple[Pipeline, BurstBuffer]:
    buffer = BurstBuffer(client, window_seconds=WINDOW)
    store = BatchStore(create_async_engine(app_dsn))
    return Pipeline(client, buffer, store, handler), buffer  # type: ignore[arg-type]


async def test_a_burst_becomes_one_processed_batch(
    client: aioredis.Redis, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    tenant_id = await _tenant(owner_conn, "pipe-1")
    seen: list[Batch] = []

    async def handler(batch: Batch) -> None:
        seen.append(batch)

    pipeline, buffer = _pipeline(client, app_dsn, handler)
    for i, body in enumerate(["supino reto", "10kg", "8 reps"]):
        await buffer.push(
            "pipe-1",
            {"message_id": f"m{i}", "tenant_id": tenant_id, "text": body},
            now=float(i),
        )

    # drain() checks readiness itself, so the clock has to have moved on.
    await client.set("deadline:pipe-1", 0)
    batch = await pipeline.flush("pipe-1")

    assert batch is not None
    assert [b.combined_text for b in seen] == ["supino reto | 10kg | 8 reps"]


async def test_a_second_worker_backs_off_instead_of_waiting(
    client: aioredis.Redis, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """Waiting would tie up a worker slot for the length of another user's LLM
    call; under load every worker ends up blocked on a handful of users."""
    tenant_id = await _tenant(owner_conn, "pipe-2")

    async def handler(batch: Batch) -> None:
        return None

    pipeline, buffer = _pipeline(client, app_dsn, handler)
    await buffer.push("pipe-2", {"message_id": "m", "tenant_id": tenant_id, "text": "x"}, now=0.0)
    await client.set("deadline:pipe-2", 0)

    async with UserLock(client, "pipe-2") as held:
        assert held.acquired
        assert await pipeline.flush("pipe-2") is None

    # Once the holder is gone the work is still there, untouched.
    assert await pipeline.flush("pipe-2") is not None


async def test_a_failing_handler_leaves_the_batch_retryable(
    client: aioredis.Redis, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The attempt is recorded before the handler runs. Counting afterwards
    means a handler that kills the process never records its attempt and the
    batch retries forever."""
    tenant_id = await _tenant(owner_conn, "pipe-3")

    async def handler(batch: Batch) -> None:
        raise RuntimeError("provider down")

    pipeline, buffer = _pipeline(client, app_dsn, handler)
    await buffer.push("pipe-3", {"message_id": "m", "tenant_id": tenant_id, "text": "x"}, now=0.0)
    await client.set("deadline:pipe-3", 0)

    with pytest.raises(RuntimeError):
        await pipeline.flush("pipe-3")

    row = await owner_conn.execute(
        text(
            "SELECT attempts, status FROM processing_batch "
            "WHERE tenant_id = :t ORDER BY id DESC LIMIT 1"
        ),
        {"t": tenant_id},
    )
    attempts, status = row.one()
    assert attempts == 1
    assert status == "pending", "a first failure must stay retryable"


async def test_an_exhausted_batch_is_marked_failed_not_retried(
    client: aioredis.Redis, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """After the last attempt the user is told (§7.3) rather than left in
    silence, and the raw text is still in raw_message either way."""
    tenant_id = await _tenant(owner_conn, "pipe-4")

    async def handler(batch: Batch) -> None:
        raise RuntimeError("still down")

    _, buffer = _pipeline(client, app_dsn, handler)
    store = BatchStore(create_async_engine(app_dsn))
    await buffer.push("pipe-4", {"message_id": "m", "tenant_id": tenant_id, "text": "x"}, now=0.0)
    await client.set("deadline:pipe-4", 0)

    # Burn the attempts the earlier tries would have consumed.
    batch = await store.create(tenant_id, [{"message_id": "m", "text": "x"}])
    assert batch is not None
    for _ in range(MAX_ATTEMPTS - 1):
        await store.mark_attempt(batch)

    pipeline_with_used_batch = Pipeline(
        client,
        buffer,
        _PrimedStore(store, batch),
        handler,  # type: ignore[arg-type]
    )
    await pipeline_with_used_batch.flush("pipe-4")

    row = await owner_conn.execute(
        text("SELECT status, error FROM processing_batch WHERE id = :i"),
        {"i": batch.id},
    )
    status, error = row.one()
    assert status == "failed"
    assert "still down" in error


class _PrimedStore:
    """Returns an existing batch so the test can control the attempt count."""

    def __init__(self, inner: BatchStore, batch: Batch) -> None:
        self._inner = inner
        self._batch = batch

    async def create(self, _tenant_id: int, _messages: list[dict[str, object]]) -> Batch:
        return self._batch

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_two_users_are_processed_concurrently(
    client: aioredis.Redis, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    a = await _tenant(owner_conn, "pipe-a")
    b = await _tenant(owner_conn, "pipe-b")
    running = 0
    peak = 0

    async def handler(batch: Batch) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1

    pipeline, buffer = _pipeline(client, app_dsn, handler)
    for name, tenant_id in (("pipe-a", a), ("pipe-b", b)):
        await buffer.push(name, {"message_id": "m", "tenant_id": tenant_id, "text": "x"}, now=0.0)
        await client.set(f"deadline:{name}", 0)

    await asyncio.gather(pipeline.flush("pipe-a"), pipeline.flush("pipe-b"))

    assert peak == 2, "different users were serialised"
