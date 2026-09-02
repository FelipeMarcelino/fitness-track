"""The outbound limiter against Redis executing its real Lua script."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import redis.asyncio as redis

from fittrack.services.outbound import RedisRateLimiter
from tests.conftest import HOST


@pytest.fixture
async def redis_client(
    redis_password: str,
    ca_file: Path,
) -> AsyncIterator[redis.Redis]:
    client = redis.Redis(
        host=HOST["redis"],
        port=6379,
        password=redis_password,
        ssl=True,
        ssl_cert_reqs="required",
        ssl_ca_certs=str(ca_file),
        ssl_check_hostname=True,
        decode_responses=True,
    )
    try:
        yield client
    finally:
        await client.aclose()


async def _near_start_of_redis_second(client: redis.Redis) -> None:
    """Keep 30 local round trips away from a sliding-window boundary."""
    _, microseconds = await client.time()
    offset = microseconds / 1_000_000
    if offset > 0.05:
        await asyncio.sleep(1.05 - offset)


async def test_real_lua_admits_thirty_distinct_chats_and_makes_the_thirty_first_wait(
    redis_client: redis.Redis,
) -> None:
    unique = secrets.token_hex(8)
    global_key = f"test:outbound:rate:global:{unique}"
    identity_base = secrets.randbelow(1_000_000_000) + 1_000_000_000
    delays: list[float] = []

    class IsolatedLimiter(RedisRateLimiter):
        GLOBAL_KEY = global_key

    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(delay)

    limiter = IsolatedLimiter(redis_client, sleep=sleep)  # type: ignore[arg-type]
    chat_keys = [f"outbound:rate:identity:{identity_base + index}" for index in range(31)]
    try:
        await _near_start_of_redis_second(redis_client)
        for index in range(30):
            await limiter.acquire(identity_base + index)
        assert delays == []

        await limiter.acquire(identity_base + 30)

        assert delays
        assert 0 < delays[0] <= 1.0
    finally:
        await redis_client.delete(global_key, *chat_keys)


async def test_real_lua_spaces_two_acquires_for_one_chat_by_at_least_one_second(
    redis_client: redis.Redis,
) -> None:
    unique = secrets.token_hex(8)
    global_key = f"test:outbound:rate:global:{unique}"
    identity_id = secrets.randbelow(1_000_000_000) + 3_000_000_000
    chat_key = f"outbound:rate:identity:{identity_id}"
    delays: list[float] = []
    tokens = iter((f"{unique}:first", f"{unique}:second", f"{unique}:third"))

    class IsolatedLimiter(RedisRateLimiter):
        GLOBAL_KEY = global_key

    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(delay)

    limiter = IsolatedLimiter(
        redis_client,  # type: ignore[arg-type]
        sleep=sleep,
        token=lambda: next(tokens),
    )
    try:
        await limiter.acquire(identity_id)
        first_score = await redis_client.zscore(global_key, f"{unique}:first")

        await limiter.acquire(identity_id)
        second_score = await redis_client.zscore(global_key, f"{unique}:third")

        assert delays
        assert first_score is not None
        assert second_score is not None
        assert second_score - first_score >= RedisRateLimiter.CHAT_INTERVAL_MS
    finally:
        await redis_client.delete(global_key, chat_key)
