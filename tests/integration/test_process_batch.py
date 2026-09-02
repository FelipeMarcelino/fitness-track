"""Batch drain and persistence contracts for Sprint 02 task S02-T05.

Spec: §4 (end-to-end flow), §17.2 (fila default), §4.1 (guarantees),
§22.2 (combined_text encrypted).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import pytest

from fittrack.security.crypto import ColumnCipher, Keyring, column_aad
from fittrack.services.batch import (
    BatchRow,
    persist_batch,
    prepare_items,
    process_batch,
)
from fittrack.services.debounce import (
    _EXTEND_LOCK,
    _RELEASE_LOCK,
    DrainResult,
)

# ─── Fakes ──────────────────────────────────────────────────────────────────


class FakeRedis:
    """In-memory Redis fake supporting lock operations for process_batch."""

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

        raise NotImplementedError("Unknown Lua script in FakeRedis")  # pragma: no cover


class FakeBatchStore:
    """In-memory batch store for testing."""

    def __init__(self) -> None:
        self._next_id = 1
        self._rows: dict[int, dict[str, object]] = {}

    async def reserve_id(self) -> int:
        row_id = self._next_id
        self._next_id += 1
        return row_id

    async def insert(
        self,
        *,
        row_id: int,
        tenant_id: int,
        message_ids: list[str],
        combined_text: bytes,
        key_version: int,
    ) -> None:
        self._rows[row_id] = {
            "id": row_id,
            "tenant_id": tenant_id,
            "message_ids": message_ids,
            "combined_text": combined_text,
            "key_version": key_version,
            "status": "pending",
            "attempts": 0,
        }

    async def get(self, *, batch_id: int, tenant_id: int) -> BatchRow | None:
        row = self._rows.get(batch_id)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        return BatchRow(
            id=row["id"],  # type: ignore[arg-type]
            tenant_id=row["tenant_id"],  # type: ignore[arg-type]
            message_ids=row["message_ids"],  # type: ignore[arg-type]
            combined_text=row["combined_text"],  # type: ignore[arg-type]
            key_version=row["key_version"],  # type: ignore[arg-type]
            status=row["status"],  # type: ignore[arg-type]
            attempts=row["attempts"],  # type: ignore[arg-type]
        )

    async def mark_done(self, *, batch_id: int, tenant_id: int) -> None:
        row = self._rows.get(batch_id)
        if row is not None and row["tenant_id"] == tenant_id:
            row["status"] = "done"
            row["attempts"] = row["attempts"] + 1  # type: ignore[operator]


@dataclass
class FakeBatchEnqueuer:
    """Records enqueue_process_batch calls."""

    calls: list[tuple[int, int]] = field(default_factory=list)

    async def enqueue_process_batch(self, *, tenant_id: int, batch_id: int) -> None:
        self.calls.append((tenant_id, batch_id))


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def test_keyring() -> Keyring:
    key = os.urandom(32)
    return Keyring(keys={1: key}, active_version=1)


@pytest.fixture
def cipher(test_keyring: Keyring) -> ColumnCipher:
    return ColumnCipher(test_keyring)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def fake_store() -> FakeBatchStore:
    return FakeBatchStore()


@pytest.fixture
def fake_enqueuer() -> FakeBatchEnqueuer:
    return FakeBatchEnqueuer()


def _make_drain(
    items: list[dict[str, object]] | None = None,
) -> DrainResult:
    if items is None:
        items = [
            {"message_id": "m1", "text": "supino 10kg 8 reps", "kind": "text"},
            {"message_id": "m2", "text": "foi facil", "kind": "text"},
        ]
    return DrainResult(batch_id="abc123", items=items)


# ─── prepare_items ─────────────────────────────────────────────────────────


class TestPrepareItems:
    def test_marks_voice_items(self) -> None:
        items: list[dict[str, object]] = [
            {"message_id": "m1", "text": "oi", "kind": "text"},
            {"message_id": "m2", "kind": "voice"},
        ]
        result, _ = prepare_items(items)
        assert result[0].get("was_audio") is None
        assert result[1]["was_audio"] is True

    def test_preserves_arrival_order(self) -> None:
        items: list[dict[str, object]] = [
            {"message_id": "m3", "text": "c"},
            {"message_id": "m1", "text": "a"},
            {"message_id": "m2", "text": "b"},
        ]
        _, message_ids = prepare_items(items)
        assert message_ids == ["m3", "m1", "m2"]

    def test_extracts_message_ids(self) -> None:
        items: list[dict[str, object]] = [
            {"message_id": "m1", "text": "hello"},
            {"message_id": "m2", "text": "world"},
        ]
        _, message_ids = prepare_items(items)
        assert message_ids == ["m1", "m2"]

    def test_handles_missing_message_id(self) -> None:
        items: list[dict[str, object]] = [
            {"text": "hello"},
            {"message_id": "m2", "text": "world"},
        ]
        _, message_ids = prepare_items(items)
        assert message_ids == ["m2"]

    def test_voice_and_text_mixed(self) -> None:
        items: list[dict[str, object]] = [
            {"message_id": "m1", "kind": "voice"},
            {"message_id": "m2", "kind": "text", "text": "hello"},
            {"message_id": "m3", "kind": "voice"},
        ]
        result, _ = prepare_items(items)
        assert result[0]["was_audio"] is True
        assert result[1].get("was_audio") is None
        assert result[2]["was_audio"] is True


# ─── persist_batch ─────────────────────────────────────────────────────────


class TestPersistBatch:
    @pytest.mark.asyncio
    async def test_returns_row_id(self, cipher: ColumnCipher, fake_store: FakeBatchStore) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        assert row_id == 1

    @pytest.mark.asyncio
    async def test_combined_text_is_encrypted(
        self, cipher: ColumnCipher, fake_store: FakeBatchStore
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None

        # The stored combined_text must be decryptable with the correct AAD
        aad = column_aad(
            tenant_id=1,
            table="processing_batch",
            column="combined_text",
            row_id=row_id,
        )
        plaintext = cipher.decrypt(row.combined_text, aad, declared_version=1)
        items = json.loads(plaintext)
        assert len(items) == 2
        assert items[0]["text"] == "supino 10kg 8 reps"

    @pytest.mark.asyncio
    async def test_wrong_aad_fails_decryption(
        self, cipher: ColumnCipher, fake_store: FakeBatchStore
    ) -> None:
        from fittrack.security.crypto import DecryptionError

        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None

        # Wrong tenant_id in AAD must fail
        wrong_aad = column_aad(
            tenant_id=999,
            table="processing_batch",
            column="combined_text",
            row_id=row_id,
        )
        with pytest.raises(DecryptionError):
            cipher.decrypt(row.combined_text, wrong_aad, declared_version=1)

    @pytest.mark.asyncio
    async def test_stores_message_ids(
        self, cipher: ColumnCipher, fake_store: FakeBatchStore
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.message_ids == ["m1", "m2"]

    @pytest.mark.asyncio
    async def test_key_version_matches_cipher(
        self, cipher: ColumnCipher, fake_store: FakeBatchStore
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.key_version == cipher.active_version

    @pytest.mark.asyncio
    async def test_voice_items_marked_before_persist(
        self, cipher: ColumnCipher, fake_store: FakeBatchStore
    ) -> None:
        items: list[dict[str, object]] = [
            {"message_id": "m1", "kind": "voice"},
            {"message_id": "m2", "kind": "text", "text": "hello"},
        ]
        drain = _make_drain(items)
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None

        aad = column_aad(
            tenant_id=1,
            table="processing_batch",
            column="combined_text",
            row_id=row_id,
        )
        plaintext = cipher.decrypt(row.combined_text, aad, declared_version=1)
        stored_items = json.loads(plaintext)
        assert stored_items[0]["was_audio"] is True
        assert stored_items[1].get("was_audio") is None

    @pytest.mark.asyncio
    async def test_status_is_pending(
        self, cipher: ColumnCipher, fake_store: FakeBatchStore
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "pending"


# ─── process_batch ─────────────────────────────────────────────────────────


class TestProcessBatch:
    @pytest.mark.asyncio
    async def test_acquires_lock_and_marks_done(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"
        assert row.attempts == 1

        # Lock should be released after processing
        assert "lock:1" not in fake_redis._data

    @pytest.mark.asyncio
    async def test_skips_already_done_batch(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        # Process once
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )
        # Process again — idempotent
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"
        # attempts should still be 1 (second call skipped)
        assert row.attempts == 1

    @pytest.mark.asyncio
    async def test_raises_on_lock_contention(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        # Pre-acquire the lock to simulate contention
        await fake_redis.set("lock:1", "other-worker", ex=120, nx=True)

        with pytest.raises(RuntimeError, match="could not acquire lock"):
            await process_batch(
                tenant_id=1,
                batch_id=row_id,
                redis=fake_redis,  # type: ignore[arg-type]
                store=fake_store,  # type: ignore[arg-type]
            )

        # Batch status unchanged
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "pending"

    @pytest.mark.asyncio
    async def test_missing_batch_returns_without_error(
        self,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        # No batch exists, should return without error
        await process_batch(
            tenant_id=1,
            batch_id=999,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )

    @pytest.mark.asyncio
    async def test_wrong_tenant_returns_without_error(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        # Try to process with wrong tenant
        await process_batch(
            tenant_id=999,
            batch_id=row_id,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )

        # Original batch unchanged
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "pending"


# ─── End-to-end: flush → persist → process ─────────────────────────────────


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_flush_persist_process_flow(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
        fake_enqueuer: FakeBatchEnqueuer,
    ) -> None:
        """Simulates the full path: drain result → persist → enqueue → process."""
        drain = _make_drain()

        # Step 1: persist batch
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        # Step 2: enqueue (simulated)
        await fake_enqueuer.enqueue_process_batch(tenant_id=1, batch_id=row_id)
        assert fake_enqueuer.calls == [(1, row_id)]

        # Step 3: process batch
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"

    @pytest.mark.asyncio
    async def test_retry_idempotency(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        """Re-processing the same batch_id does not duplicate work."""
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        # First processing
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )
        # Retry — should be idempotent
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,  # type: ignore[arg-type]
            store=fake_store,  # type: ignore[arg-type]
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"
        assert row.attempts == 1  # not incremented on skip
