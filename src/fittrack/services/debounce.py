"""Buffer drain and debounce for the tenant message pipeline.

Spec: §4 (end-to-end flow), §17.1 (Redis keys), §17.3 (atomic drain via RENAME).

The ingress (RedisTenantBuffer in webhook.py) appends to ``buffer:{tenant_id}``
without acquiring a lock — it must respond to the channel in under 200 ms.
This module owns the worker-side drain: check debounce, acquire the per-tenant
lock, RENAME the buffer atomically and return the items.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

LOCK_TTL_S = 120
LOCK_EXTEND_INTERVAL_S = 30
LOCK_RETRY_DELAY_S = 5


# ─── Ports ──────────────────────────────────────────────────────────────────


class RedisWorkerClient(Protocol):
    """The Redis surface the worker drain path requires.

    The signatures describe the subset used by the drain logic.  At the
    wiring boundary (worker.py), ``ArqRedis`` is passed with a type-ignore
    because ``redis.asyncio`` returns ``Awaitable`` where Protocols expect
    ``Coroutine`` — the same impedance mismatch every Protocol-based Redis
    port in this codebase has.
    """

    async def get(self, name: str) -> str | bytes | None: ...

    async def set(self, name: str, value: str, *, ex: int, nx: bool = False) -> bool | None: ...

    async def lrange(self, name: str, start: int, end: int) -> list[bytes]: ...

    async def delete(self, *names: str) -> int: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> object: ...


class FlushScheduler(Protocol):
    """Schedule a future flush_check without exposing ARQ to the drain logic."""

    async def schedule_flush_check(self, *, tenant_id: int, delay_s: int) -> None:
        """Enqueue using the stable job id ``flush:{tenant_id}``."""


# ─── Value objects ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DrainResult:
    """The items atomically drained from a tenant buffer."""

    batch_id: str
    items: list[dict[str, object]]


# ─── Lua scripts ────────────────────────────────────────────────────────────

_RELEASE_LOCK = """\
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_EXTEND_LOCK = """\
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_ATOMIC_DRAIN = """\
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('RENAME', KEYS[1], KEYS[2])
  return 1
end
return 0
"""


# ─── Lock ───────────────────────────────────────────────────────────────────


@asynccontextmanager
async def tenant_lock(
    redis: RedisWorkerClient,
    tenant_id: int,
    *,
    ttl_s: int = LOCK_TTL_S,
    extend_interval_s: int = LOCK_EXTEND_INTERVAL_S,
) -> AsyncIterator[str | None]:
    """Acquire a per-tenant lock with auto-extend (spec §17.3).

    Yields the lock token if acquired, ``None`` if the lock is already held.
    Auto-extend renews the TTL every *extend_interval_s* while the caller
    holds the context, so a long-running batch cannot lose its lock.
    """
    lock_key = f"lock:{tenant_id}"
    token = uuid.uuid4().hex
    acquired = await redis.set(lock_key, token, ex=ttl_s, nx=True)
    if not acquired:
        yield None
        return

    async def _extend() -> None:
        while True:
            await asyncio.sleep(extend_interval_s)
            extended = await redis.eval(_EXTEND_LOCK, 1, lock_key, token, str(ttl_s))
            if not extended:
                break  # pragma: no cover — lost ownership, stop extending

    extend_task = asyncio.create_task(_extend())
    try:
        yield token
    finally:
        extend_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await extend_task
        await redis.eval(_RELEASE_LOCK, 1, lock_key, token)


# ─── Drain ──────────────────────────────────────────────────────────────────


async def drain_buffer(
    redis: RedisWorkerClient,
    tenant_id: int,
) -> DrainResult | None:
    """Atomically drain the tenant buffer via RENAME (spec §17.3).

    Never ``LRANGE+DEL`` on ``buffer:`` — the ingress writes without a lock
    and a message that arrives between the two commands would be deleted
    without entering the batch.
    """
    batch_id = uuid.uuid4().hex
    buffer_key = f"buffer:{tenant_id}"
    drain_key = f"drain:{tenant_id}:{batch_id}"

    renamed = await redis.eval(_ATOMIC_DRAIN, 2, buffer_key, drain_key)
    if not renamed:
        return None

    items_raw = await redis.lrange(drain_key, 0, -1)
    await redis.delete(drain_key)

    if not items_raw:
        return None  # pragma: no cover — RENAME succeeded so the list is non-empty

    items: list[dict[str, object]] = [json.loads(item) for item in items_raw]
    return DrainResult(batch_id=batch_id, items=items)


# ─── Flush check ────────────────────────────────────────────────────────────


async def flush_check(
    *,
    tenant_id: int,
    redis: RedisWorkerClient,
    scheduler: FlushScheduler,
    debounce_window_s: int,
) -> DrainResult | None:
    """The debounce gate: re-enqueue while the tenant types, drain when silent.

    Returns the drained items when the buffer is flushed successfully, or
    ``None`` when the check was re-enqueued or the buffer was empty.

    §17.1 debounce key: ``debounce:{tenant_id}`` with TTL equal to
    *debounce_window_s*.  Renewed by the ingress on every message.

    §17.3 lock: ``lock:{tenant_id}`` with TTL 120s and auto-extend.
    """
    # Step 1: debounce still active → user is still typing
    if await redis.get(f"debounce:{tenant_id}") is not None:
        await scheduler.schedule_flush_check(
            tenant_id=tenant_id,
            delay_s=debounce_window_s,
        )
        return None

    # Step 2: acquire lock — another worker may already be processing
    async with tenant_lock(redis, tenant_id) as token:
        if token is None:
            await scheduler.schedule_flush_check(
                tenant_id=tenant_id,
                delay_s=LOCK_RETRY_DELAY_S,
            )
            return None

        # Step 3: atomic drain
        return await drain_buffer(redis, tenant_id)
