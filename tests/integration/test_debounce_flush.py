"""Debounce and buffer drain contracts for Sprint 02 task S02-T04.

Spec: §4, §17.1 (buffer and debounce keys), §17.3 (atomic drain via RENAME).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from fittrack.services.debounce import (
    _ATOMIC_DRAIN,
    _EXTEND_LOCK,
    _RELEASE_LOCK,
    LOCK_RETRY_DELAY_S,
    drain_buffer,
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

        if script is _ATOMIC_DRAIN:
            src, dst = keys[0], keys[1]
            if src in self._data:
                self._data[dst] = self._data.pop(src)
                self._ttls.pop(src, None)
                return 1
            return 0

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
    """The temporary drain:{tenant_id}:{batch_id} key must not survive."""
    redis = FakeRedis()
    scheduler = FakeScheduler()
    await redis.rpush("buffer:1", _buffer_item(1))

    result = await flush_check(tenant_id=1, redis=redis, scheduler=scheduler, debounce_window_s=10)

    assert result is not None
    drain_keys = [k for k in redis._data if k.startswith("drain:")]
    assert drain_keys == []


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
# drain_buffer — isolated tests
# ═══════════════════════════════════════════════════════════════════════════


async def test_drain_buffer_returns_items_in_order() -> None:
    redis = FakeRedis()
    await redis.rpush("buffer:42", _buffer_item(10), _buffer_item(20), _buffer_item(30))

    result = await drain_buffer(redis, tenant_id=42)

    assert result is not None
    assert [item["raw_message_id"] for item in result.items] == [10, 20, 30]


async def test_drain_buffer_returns_none_when_buffer_absent() -> None:
    redis = FakeRedis()

    result = await drain_buffer(redis, tenant_id=42)

    assert result is None


async def test_drain_buffer_generates_unique_batch_id() -> None:
    redis = FakeRedis()
    await redis.rpush("buffer:1", _buffer_item(1))
    result_a = await drain_buffer(redis, tenant_id=1)

    await redis.rpush("buffer:1", _buffer_item(2))
    result_b = await drain_buffer(redis, tenant_id=1)

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
