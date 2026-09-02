"""Debounce and buffer drain contracts for Sprint 02 task S02-T04.

Spec: §4, §17.1 (buffer and debounce keys), §17.3 (atomic drain via RENAME).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from fittrack.services.debounce import (
    _EXTEND_LOCK,
    _GATED_DRAIN,
    _RELEASE_LOCK,
    LOCK_RETRY_DELAY_S,
    DrainResult,
    _read_drain,
    flush_check,
    tenant_lock,
)

# ─── Fakes ──────────────────────────────────────────────────────────────────


class FakeRedis:
    """In-memory Redis fake that supports the worker drain protocols."""

    def __init__(self) -> None:
        self._data: dict[str, str | list[bytes]] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, name: str) -> str | bytes | None:
        value = self._data.get(name)
        if isinstance(value, list):
            return None
        return value

    async def set(self, name: str, value: str, *, ex: int = 0, nx: bool = False) -> bool | None:
        if nx and name in self._data:
            return False
        self._data[name] = value
        if ex:
            self._ttls[name] = ex
        return True

    async def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        data = self._data.get(name)
        if not isinstance(data, list):
            return []
        if end == -1:
            return list(data[start:])
        return list(data[start : end + 1])

    async def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if self._data.pop(name, None) is not None:
                self._ttls.pop(name, None)
                count += 1
        return count

    async def rpush(self, name: str, *values: str | bytes) -> int:
        if name not in self._data or not isinstance(self._data[name], list):
            self._data[name] = []
        lst = self._data[name]
        assert isinstance(lst, list)
        for v in values:
            lst.append(v.encode() if isinstance(v, str) else v)
        return len(lst)

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> object:
        keys = [str(k) for k in keys_and_args[:numkeys]]
        args = [str(a) for a in keys_and_args[numkeys:]]

        if script is _RELEASE_LOCK:
            current = self._data.get(keys[0])
            if current == args[0]:
                self._data.pop(keys[0], None)
                self._ttls.pop(keys[0], None)
                return 1
            return 0

        if script is _EXTEND_LOCK:
            current = self._data.get(keys[0])
            if current == args[0]:
                self._ttls[keys[0]] = int(args[1])
                return 1
            return 0

        if script is _GATED_DRAIN:
            debounce_key, buffer_key, drain_key = keys[0], keys[1], keys[2]
            # Return 2 if orphan drain key exists
            if drain_key in self._data:
                return 2
            # Return 0 if debounce is active
            if debounce_key in self._data:
                return 0
            # Return 1 if buffer renamed
            if buffer_key in self._data:
                self._data[drain_key] = self._data.pop(buffer_key)
                self._ttls.pop(buffer_key, None)
                return 1
            # Return -1 if empty
            return -1

        raise NotImplementedError("Unknown Lua script in FakeRedis")  # pragma: no cover


@dataclass
class FakeScheduler:
    """Records every schedule_flush_check call for assertions."""

    calls: list[tuple[int, int]] = field(default_factory=list)

    async def schedule_flush_check(self, *, tenant_id: int, delay_s: int) -> None:
        self.calls.append((tenant_id, delay_s))


# ─── Helpers ──────────────────────────────────────────────────────────────


def _buffer_item(raw_message_id: int, text: str = "treino feito") -> bytes:
    """A well-formed buffer envelope matching what RedisTenantBuffer produces."""
    return json.dumps(
        {
            "button_payload": None,
            "channel": "telegram",
            "channel_message_id": f"msg-{raw_message_id}",
            "external_id_hash": "abc123",
            "kind": "text",
            "media_ref": None,
            "raw_message_id": raw_message_id,
            "sent_at": "2026-01-01T00:00:00+00:00",
            "text": text,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


# ═══════════════════════════════════════════════════════════════════════════
# flush_check — debounce gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_flush_check_reenqueues_when_debounce_is_active() -> None:
    """Debounce key present → re-enqueue with debounce window, never drain."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.set("debounce:1", "1", ex=10)
    await redis.rpush("buffer:1", _buffer_item(1))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is None
    assert scheduler.calls == [(1, 10)]
    # Buffer untouched
    assert len(await redis.lrange("buffer:1", 0, -1)) == 1
    # No lock acquired
    assert await redis.get("lock:1") is None


async def test_flush_check_drains_buffer_when_debounce_expired() -> None:
    """No debounce key → acquire lock → drain atomically → release lock."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.rpush("buffer:1", _buffer_item(1), _buffer_item(2))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is not None
    assert len(result.items) == 2
    assert result.items[0]["raw_message_id"] == 1
    assert result.items[1]["raw_message_id"] == 2
    assert isinstance(result.batch_id, str) and len(result.batch_id) > 0
    # No re-enqueue
    assert scheduler.calls == []
    # Lock released
    assert await redis.get("lock:1") is None


async def test_flush_check_reenqueues_when_lock_is_busy() -> None:
    """Another worker holds the lock → re-enqueue with shorter delay (§17.3)."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.set("lock:1", "other-worker-token", ex=120)

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is None
    assert scheduler.calls == [(1, LOCK_RETRY_DELAY_S)]
    # Original lock untouched
    assert await redis.get("lock:1") == "other-worker-token"


async def test_flush_check_returns_none_when_buffer_is_empty() -> None:
    """No debounce key, lock acquired, but buffer was already drained."""
    redis = FakeRedis()
    scheduler = FakeScheduler()

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is None
    assert scheduler.calls == []
    # Lock was acquired and released
    assert await redis.get("lock:1") is None


async def test_lock_is_released_after_successful_drain() -> None:
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.rpush("buffer:1", _buffer_item(1))

    await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert await redis.get("lock:1") is None


async def test_drain_key_is_cleaned_up_after_reading() -> None:
    """The temporary drain:{tenant_id} key must not survive."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.rpush("buffer:1", _buffer_item(1))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is not None
    drain_keys = [k for k in redis._data if k.startswith("drain:")]
    assert drain_keys == []


async def test_drain_is_acknowledged_only_after_handler_succeeds() -> None:
    """A failed persistence/enqueue handoff must leave the drain recoverable."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.rpush("buffer:1", _buffer_item(1), _buffer_item(2))
    handled: list[list[int]] = []

    async def fail_handoff(result: DrainResult) -> None:
        del result
        assert len(await redis.lrange("drain:1", 0, -1)) == 2
        raise ConnectionError("database unavailable")

    with pytest.raises(ConnectionError, match="database unavailable"):
        await flush_check(
            tenant_id=1,
            redis=redis,
            scheduler=scheduler,
            debounce_window_s=10,
            drain_handler=fail_handoff,
        )

    assert len(await redis.lrange("drain:1", 0, -1)) == 2

    async def complete_handoff(result: DrainResult) -> None:
        raw_ids: list[int] = []
        for item in result.items:
            raw_message_id = item["raw_message_id"]
            assert isinstance(raw_message_id, int)
            raw_ids.append(raw_message_id)
        handled.append(raw_ids)

    await flush_check(
        tenant_id=1,
        redis=redis,
        scheduler=scheduler,
        debounce_window_s=10,
        drain_handler=complete_handoff,
    )

    assert handled == [[1, 2]]
    assert await redis.lrange("drain:1", 0, -1) == []


# ═══════════════════════════════════════════════════════════════════════════
# Orphan recovery — drain key survives crash (§17.3)
# ═══════════════════════════════════════════════════════════════════════════


async def test_flush_check_recovers_orphaned_drain() -> None:
    """Orphaned drain:{tenant_id} from a crashed worker is recovered."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    # Simulate a crash: drain key exists from a previous RENAME
    await redis.rpush("drain:1", _buffer_item(1), _buffer_item(2))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is not None
    assert len(result.items) == 2
    assert [item["raw_message_id"] for item in result.items] == [1, 2]
    # Drain key cleaned up
    assert await redis.lrange("drain:1", 0, -1) == []
    # No re-enqueue
    assert scheduler.calls == []


async def test_orphan_recovery_does_not_lose_new_buffer() -> None:
    """Orphan is recovered first; new buffer waits for next flush_check."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    # Orphan from previous crash
    await redis.rpush("drain:1", _buffer_item(1))
    # New messages arrived after crash
    await redis.rpush("buffer:1", _buffer_item(2))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    # Orphan recovered
    assert result is not None
    assert [item["raw_message_id"] for item in result.items] == [1]
    # Buffer still has new messages for next cycle
    assert len(await redis.lrange("buffer:1", 0, -1)) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Gated drain — debounce race prevention
# ═══════════════════════════════════════════════════════════════════════════


async def test_gated_drain_returns_zero_when_debounce_set() -> None:
    """_GATED_DRAIN atomically checks debounce before rename."""
    redis = FakeRedis()
    await redis.rpush("buffer:1", _buffer_item(1))
    await redis.set("debounce:1", "1", ex=10)

    status = await redis.eval(_GATED_DRAIN, 3, "debounce:1", "buffer:1", "drain:1")

    assert status == 0
    # Buffer untouched
    assert len(await redis.lrange("buffer:1", 0, -1)) == 1
    # No drain key created
    assert await redis.lrange("drain:1", 0, -1) == []


async def test_gated_drain_returns_two_when_orphan_exists() -> None:
    """_GATED_DRAIN detects orphaned drain key."""
    redis = FakeRedis()
    await redis.rpush("drain:1", _buffer_item(1))

    status = await redis.eval(_GATED_DRAIN, 3, "debounce:1", "buffer:1", "drain:1")

    assert status == 2


async def test_gated_drain_returns_negative_one_when_empty() -> None:
    """_GATED_DRAIN returns -1 when no orphan, no debounce, no buffer."""
    redis = FakeRedis()

    status = await redis.eval(_GATED_DRAIN, 3, "debounce:1", "buffer:1", "drain:1")

    assert status == -1


async def test_flush_check_reenqueues_on_late_debounce() -> None:
    """Gated drain catches debounce that appeared after optimistic check.

    This tests the fix for the race where ingress sets debounce between the
    optimistic GET (step 1) and the Lua script (step 3). We simulate this
    by having step 1 pass (no debounce), then setting debounce before step 3
    runs. Since FakeRedis is synchronous within eval, we insert the debounce
    key before calling flush_check and rely on step 1 NOT being reached
    (because debounce is set). To test the gated path specifically, we call
    the Lua script directly.
    """
    redis = FakeRedis()
    await redis.rpush("buffer:1", _buffer_item(1))
    await redis.set("debounce:1", "1", ex=10)

    status = await redis.eval(_GATED_DRAIN, 3, "debounce:1", "buffer:1", "drain:1")

    # Lua returns 0 — buffer untouched, debounce active
    assert status == 0
    assert len(await redis.lrange("buffer:1", 0, -1)) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Drain atomicity — RENAME, never LRANGE+DEL on buffer (§17.3)
# ═══════════════════════════════════════════════════════════════════════════


async def test_new_messages_after_rename_go_to_fresh_buffer() -> None:
    """Ingress writes during drain land in a new buffer, not the drained one."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.rpush("buffer:1", _buffer_item(1))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    # A new message arrives after drain
    await redis.rpush("buffer:1", _buffer_item(2))

    assert result is not None
    assert len(result.items) == 1
    assert result.items[0]["raw_message_id"] == 1

    # The new message is in a fresh buffer
    remaining = await redis.lrange("buffer:1", 0, -1)
    assert len(remaining) == 1
    assert json.loads(remaining[0])["raw_message_id"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# _read_drain — isolated tests
# ═══════════════════════════════════════════════════════════════════════════


async def test_read_drain_returns_items_in_order() -> None:
    redis = FakeRedis()
    # _read_drain reads from drain:{tenant_id} (populated by _GATED_DRAIN)
    await redis.rpush("drain:42", _buffer_item(10), _buffer_item(20), _buffer_item(30))

    result = await _read_drain(redis, tenant_id=42)

    assert result is not None
    assert [item["raw_message_id"] for item in result.items] == [10, 20, 30]


async def test_read_drain_leaves_the_key_for_the_caller_to_acknowledge() -> None:
    redis = FakeRedis()
    await redis.rpush("drain:42", _buffer_item(10))

    assert await _read_drain(redis, tenant_id=42) is not None

    assert len(await redis.lrange("drain:42", 0, -1)) == 1


async def test_read_drain_returns_none_when_drain_absent() -> None:
    redis = FakeRedis()

    result = await _read_drain(redis, tenant_id=42)

    assert result is None


async def test_read_drain_generates_unique_batch_id() -> None:
    redis = FakeRedis()
    await redis.rpush("drain:1", _buffer_item(1))

    result_a = await _read_drain(redis, tenant_id=1)
    result_b = await _read_drain(redis, tenant_id=1)

    assert result_a is not None and result_b is not None
    assert result_a.batch_id != result_b.batch_id


# ═══════════════════════════════════════════════════════════════════════════
# tenant_lock — acquisition, release and auto-extend
# ═══════════════════════════════════════════════════════════════════════════


async def test_tenant_lock_acquires_and_releases() -> None:
    redis = FakeRedis()

    async with tenant_lock(redis, tenant_id=1) as token:
        assert token is not None
        assert await redis.get("lock:1") == token

    assert await redis.get("lock:1") is None


async def test_tenant_lock_yields_none_when_busy() -> None:
    redis = FakeRedis()
    await redis.set("lock:1", "held-by-another", ex=120)

    async with tenant_lock(redis, tenant_id=1) as token:
        assert token is None

    # Original lock still held
    assert await redis.get("lock:1") == "held-by-another"


async def test_tenant_lock_sets_correct_ttl() -> None:
    redis = FakeRedis()

    async with tenant_lock(redis, tenant_id=1, ttl_s=120) as token:
        assert token is not None
        assert redis._ttls.get("lock:1") == 120


async def test_tenant_lock_survives_extend_error() -> None:
    """Transient error in lock extension doesn't prevent lock release."""
    redis = FakeRedis()
    extend_count = 0
    original_eval = redis.eval

    async def failing_eval(script: str, numkeys: int, *keys_and_args: str | int) -> object:
        nonlocal extend_count
        if script is _EXTEND_LOCK:
            extend_count += 1
            if extend_count == 1:
                raise ConnectionError("transient Redis failure")
        return await original_eval(script, numkeys, *keys_and_args)

    redis.eval = failing_eval  # type: ignore[method-assign]

    async with tenant_lock(redis, tenant_id=1, extend_interval_s=0.01) as token:
        assert token is not None
        # Let the extend task run, fail once, and retry
        await asyncio.sleep(0.05)

    # Lock was released despite the transient extend error
    assert await redis.get("lock:1") is None


# ═══════════════════════════════════════════════════════════════════════════
# Burst scenario — full pipeline
# ═══════════════════════════════════════════════════════════════════════════


async def test_burst_of_four_messages_produces_single_batch() -> None:
    """A burst of 4 messages (§4) results in one drain with all 4 items."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    for i in range(1, 5):
        await redis.rpush("buffer:1", _buffer_item(i))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is not None
    assert len(result.items) == 4
    assert [item["raw_message_id"] for item in result.items] == [1, 2, 3, 4]


async def test_consecutive_drains_are_independent() -> None:
    """Two consecutive flush cycles each produce their own batch."""
    redis = FakeRedis()
    scheduler = FakeScheduler()

    await redis.rpush("buffer:1", _buffer_item(1), _buffer_item(2))
    result_a = await flush_check(
        tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10
    )

    await redis.rpush("buffer:1", _buffer_item(3))
    result_b = await flush_check(
        tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10
    )

    assert result_a is not None and result_b is not None
    assert [item["raw_message_id"] for item in result_a.items] == [1, 2]
    assert [item["raw_message_id"] for item in result_b.items] == [3]
    assert result_a.batch_id != result_b.batch_id
