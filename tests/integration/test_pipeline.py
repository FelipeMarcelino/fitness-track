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


async def test_the_messages_survive_a_crash_before_the_batch_is_written(
    client: aioredis.Redis, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The drain key must outlive the database insert.

    If Redis drops the burst first and the worker dies before the batch
    commits, the messages exist nowhere: not in the buffer, not in
    processing_batch, and the reclaimer has nothing to find. That is silent
    loss of a workout the user reported.
    """
    tenant_id = await _tenant(owner_conn, "pipe-crash")

    class DyingStore(BatchStore):
        async def create(self, _tid: int, _messages: list[dict[str, object]]) -> Batch | None:
            raise RuntimeError("postgres went away mid-insert")

    buffer = BurstBuffer(client, window_seconds=WINDOW)
    store = DyingStore(create_async_engine(app_dsn))

    async def handler(batch: Batch) -> None:  # pragma: no cover - never reached
        raise AssertionError("the batch was never created")

    pipeline = Pipeline(client, buffer, store, handler)
    await buffer.push(
        "pipe-crash",
        {"message_id": "m0", "tenant_id": tenant_id, "text": "supino 80kg 8"},
        now=0,
    )

    with pytest.raises(RuntimeError):
        await pipeline.flush("pipe-crash")

    # Still recoverable: the burst is sitting in its drain key, which is
    # exactly what reclaim_orphans scans for.
    stranded = [k async for k in client.scan_iter(match="drain:pipe-crash:*")]
    assert stranded, "the drain was cleared before the batch was durable"

    # And the reclaimer does put it back, once the lease is gone.
    for key in stranded:
        _, _, batch_id = key[len("drain:") :].rpartition(":")
        await client.delete(f"drainlease:pipe-crash:{batch_id}")
    assert await buffer.reclaim_orphans() == 1
    assert await client.llen(buffer.buffer_key("pipe-crash")) == 1


async def test_a_failed_batch_is_retried_from_the_database(
    client: aioredis.Redis, app_dsn: str, owner_conn: AsyncConnection
) -> None:
    """The retry job carries only the user id, and by then Redis is empty.

    Draining again finds nothing, so unless the pending batch is loaded back
    out of processing_batch the failure is permanent -- the retry budget in
    §17 would count attempts against a batch nobody ever runs again.
    """
    tenant_id = await _tenant(owner_conn, "pipe-retry")
    attempts: list[str] = []

    async def flaky(batch: Batch) -> None:
        attempts.append(batch.combined_text)
        if len(attempts) == 1:
            raise RuntimeError("the model timed out")

    pipeline, buffer = _pipeline(client, app_dsn, flaky)
    await buffer.push(
        "pipe-retry",
        {"message_id": "r0", "tenant_id": tenant_id, "text": "agachamento 100kg 5"},
        now=0,
    )

    with pytest.raises(RuntimeError):
        await pipeline.flush("pipe-retry")

    assert await client.llen(buffer.buffer_key("pipe-retry")) == 0, "the buffer should be empty"

    # Same call the requeued job makes: nothing buffered, everything pending.
    batch = await pipeline.flush("pipe-retry")

    assert batch is not None
    assert attempts == ["agachamento 100kg 5", "agachamento 100kg 5"]
    assert batch.attempts == 2
