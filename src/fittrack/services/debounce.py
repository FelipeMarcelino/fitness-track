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
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

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


DrainHandler = Callable[[DrainResult], Awaitable[None]]


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

_GATED_DRAIN = """\
-- KEYS[1] = debounce:{tenant_id}
-- KEYS[2] = buffer:{tenant_id}
-- KEYS[3] = drain:{tenant_id}
-- Returns: 2 = orphan exists, 1 = renamed, 0 = debounce active, -1 = empty
if redis.call('EXISTS', KEYS[3]) == 1 then
  return 2
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('RENAME', KEYS[2], KEYS[3])
  return 1
end
return -1
"""

# Return codes from _GATED_DRAIN
_DRAIN_ORPHAN = 2
_DRAIN_RENAMED = 1
_DRAIN_DEBOUNCE = 0
_DRAIN_EMPTY = -1


# ─── Lock ───────────────────────────────────────────────────────────────────


@asynccontextmanager
async def tenant_lock(
    redis: RedisWorkerClient,
    tenant_id: int,
    *,
    ttl_s: int = LOCK_TTL_S,
    extend_interval_s: float = LOCK_EXTEND_INTERVAL_S,
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
            try:
                extended = await redis.eval(_EXTEND_LOCK, 1, lock_key, token, str(ttl_s))
                if not extended:
                    break  # lost ownership, stop extending
            except Exception:
                # Transient Redis error — the lock TTL is still counting down.
                # Retry on next interval; if TTL expires the lock is released
                # naturally and another worker can take over (correct by design).
                logger.warning(
                    "lock extend failed for tenant %s, will retry",
                    tenant_id,
                    exc_info=True,
                )

    extend_task = asyncio.create_task(_extend())
    try:
        yield token
    finally:
        extend_task.cancel()
        with contextlib.suppress(BaseException):
            await extend_task
        await redis.eval(_RELEASE_LOCK, 1, lock_key, token)


# ─── Drain ──────────────────────────────────────────────────────────────────


async def drain_buffer(
    redis: RedisWorkerClient,
    tenant_id: int,
) -> DrainResult | None:
    """Read and delete the ``drain:{tenant_id}`` key.

    Called after ``_GATED_DRAIN`` has renamed the buffer or found an orphan.
    Uses a deterministic key ``drain:{tenant_id}`` (no batch_id suffix) so
    that a crash between RENAME and DELETE leaves a recoverable orphan that
    ``_GATED_DRAIN`` detects on the next run (spec §17.3).
    """
    drain_key = f"drain:{tenant_id}"

    items_raw = await redis.lrange(drain_key, 0, -1)
    if not items_raw:
        return None  # pragma: no cover — caller ensures drain key exists

    items: list[dict[str, object]] = [json.loads(item) for item in items_raw]
    result = DrainResult(batch_id=uuid.uuid4().hex, items=items)
    await redis.delete(drain_key)
    return result


async def _read_drain(redis: RedisWorkerClient, tenant_id: int) -> DrainResult | None:
    """Read a drain without acknowledging it.

    The worker uses this lease-like operation so a database or queue failure
    leaves ``drain:{tenant_id}`` available to the next ``flush_check`` retry.
    """
    items_raw = await redis.lrange(f"drain:{tenant_id}", 0, -1)
    if not items_raw:
        return None
    items: list[dict[str, object]] = [json.loads(item) for item in items_raw]
    return DrainResult(batch_id=uuid.uuid4().hex, items=items)


# ─── Flush check ────────────────────────────────────────────────────────────


async def flush_check(
    *,
    tenant_id: int,
    redis: RedisWorkerClient,
    scheduler: FlushScheduler,
    debounce_window_s: int,
    drain_handler: DrainHandler | None = None,
) -> DrainResult | None:
    """The debounce gate: re-enqueue while the tenant types, drain when silent.

    Returns the drained items when the buffer is flushed successfully, or
    ``None`` when the check was re-enqueued or the buffer was empty.

    §17.1 debounce key: ``debounce:{tenant_id}`` with TTL equal to
    *debounce_window_s*.  Renewed by the ingress on every message.

    §17.3 lock: ``lock:{tenant_id}`` with TTL 120s and auto-extend.
    """
    # Step 1: optimistic debounce check — fast path, avoids lock acquisition
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

        # Step 3: gated drain — atomically re-checks debounce, detects
        # orphans from a previous crash, and renames the buffer.
        debounce_key = f"debounce:{tenant_id}"
        buffer_key = f"buffer:{tenant_id}"
        drain_key = f"drain:{tenant_id}"

        status = await redis.eval(_GATED_DRAIN, 3, debounce_key, buffer_key, drain_key)

        if status == _DRAIN_DEBOUNCE:
            # Debounce became active between step 1 and step 3 (race closed)
            await scheduler.schedule_flush_check(
                tenant_id=tenant_id,
                delay_s=debounce_window_s,
            )
            return None

        if status == _DRAIN_EMPTY:
            return None

        # Keep the drain until persistence and enqueue both succeed. An
        # exception deliberately leaves it behind for the next retry.
        result = await _read_drain(redis, tenant_id)
        if result is None:  # pragma: no cover — the key is expected to contain items
            await redis.delete(drain_key)
            return None
        if drain_handler is not None:
            await drain_handler(result)
        await redis.delete(drain_key)
        return result
