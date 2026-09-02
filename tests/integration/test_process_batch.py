"""Batch drain and persistence contracts for Sprint 02 task S02-T05.

Spec: §4 (end-to-end flow), §17.2 (fila default), §4.1 (guarantees),
§22.2 (combined_text encrypted).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import asyncpg
import pytest
from arq import ArqRedis, Retry
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import fittrack.worker as worker_module
from fittrack.db.engine import session_factory, split_ssl_arguments
from fittrack.security.crypto import ColumnCipher, Keyring, column_aad
from fittrack.services.batch import (
    BatchLockContentionError,
    BatchRow,
    PostgresBatchStore,
    persist_batch,
    prepare_items,
    process_batch,
)
from fittrack.services.debounce import (
    _EXTEND_LOCK,
    _RELEASE_LOCK,
    DrainResult,
)
from fittrack.settings import Settings
from tests.conftest import CA_FILE

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
        self._rows: dict[int, StoredBatch] = {}

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
        self._rows[row_id] = StoredBatch(
            id=row_id,
            tenant_id=tenant_id,
            message_ids=message_ids,
            combined_text=combined_text,
            key_version=key_version,
        )

    async def find_by_message_ids(
        self, *, tenant_id: int, message_ids: list[str]
    ) -> BatchRow | None:
        for row in self._rows.values():
            if row.tenant_id == tenant_id and row.message_ids == message_ids:
                return self._as_batch_row(row)
        return None

    async def get(self, *, batch_id: int, tenant_id: int) -> BatchRow | None:
        row = self._rows.get(batch_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return self._as_batch_row(row)

    @staticmethod
    def _as_batch_row(row: StoredBatch) -> BatchRow:
        return BatchRow(
            id=row.id,
            tenant_id=row.tenant_id,
            message_ids=row.message_ids,
            combined_text=row.combined_text,
            key_version=row.key_version,
            status=row.status,
            attempts=row.attempts,
        )

    async def mark_done(self, *, batch_id: int, tenant_id: int) -> None:
        row = self._rows.get(batch_id)
        if row is not None and row.tenant_id == tenant_id:
            row.status = "done"
            row.attempts += 1


@dataclass
class StoredBatch:
    id: int
    tenant_id: int
    message_ids: list[str]
    combined_text: bytes
    key_version: int
    status: str = "pending"
    attempts: int = 0


@dataclass
class FakeBatchEnqueuer:
    """Records enqueue_process_batch calls."""

    calls: list[tuple[int, int, int]] = field(default_factory=list)

    async def enqueue_process_batch(
        self, *, tenant_id: int, batch_id: int, delay_s: int = 0
    ) -> None:
        self.calls.append((tenant_id, batch_id, delay_s))


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
            {
                "channel_message_id": "m1",
                "raw_message_id": 101,
                "text": "supino 10kg 8 reps",
                "kind": "text",
            },
            {
                "channel_message_id": "m2",
                "raw_message_id": 102,
                "text": "foi facil",
                "kind": "text",
            },
        ]
    return DrainResult(batch_id="abc123", items=items)


# ─── prepare_items ─────────────────────────────────────────────────────────


class TestPrepareItems:
    def test_marks_voice_items(self) -> None:
        items: list[dict[str, object]] = [
            {"channel_message_id": "m1", "raw_message_id": 101, "text": "oi", "kind": "text"},
            {"channel_message_id": "m2", "raw_message_id": 102, "kind": "voice"},
        ]
        result, _ = prepare_items(items)
        assert result[0].get("was_audio") is None
        assert result[1]["was_audio"] is True

    def test_preserves_arrival_order(self) -> None:
        items: list[dict[str, object]] = [
            {"channel_message_id": "m3", "raw_message_id": 103, "text": "c"},
            {"channel_message_id": "m1", "raw_message_id": 101, "text": "a"},
            {"channel_message_id": "m2", "raw_message_id": 102, "text": "b"},
        ]
        _, message_ids = prepare_items(items)
        assert message_ids == ["103", "101", "102"]

    def test_extracts_message_ids(self) -> None:
        items: list[dict[str, object]] = [
            {"channel_message_id": "m1", "raw_message_id": 101, "text": "hello"},
            {"channel_message_id": "m2", "raw_message_id": 102, "text": "world"},
        ]
        _, message_ids = prepare_items(items)
        assert message_ids == ["101", "102"]

    def test_handles_missing_message_id(self) -> None:
        items: list[dict[str, object]] = [
            {"text": "hello"},
            {"channel_message_id": "m2", "raw_message_id": 102, "text": "world"},
        ]
        _, message_ids = prepare_items(items)
        assert message_ids == ["102"]

    def test_voice_and_text_mixed(self) -> None:
        items: list[dict[str, object]] = [
            {"channel_message_id": "m1", "raw_message_id": 101, "kind": "voice"},
            {
                "channel_message_id": "m2",
                "raw_message_id": 102,
                "kind": "text",
                "text": "hello",
            },
            {"channel_message_id": "m3", "raw_message_id": 103, "kind": "voice"},
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
        assert row.message_ids == ["101", "102"]

    @pytest.mark.asyncio
    async def test_retry_reuses_batch_for_same_drain(
        self, cipher: ColumnCipher, fake_store: FakeBatchStore
    ) -> None:
        drain = _make_drain()

        first_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)
        retry_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        assert retry_id == first_id
        assert len(fake_store._rows) == 1

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
            {"channel_message_id": "m1", "raw_message_id": 101, "kind": "voice"},
            {
                "channel_message_id": "m2",
                "raw_message_id": 102,
                "kind": "text",
                "text": "hello",
            },
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
            redis=fake_redis,
            store=fake_store,
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
            redis=fake_redis,
            store=fake_store,
        )
        # Process again — idempotent
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,
            store=fake_store,
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"
        # attempts should still be 1 (second call skipped)
        assert row.attempts == 1

    @pytest.mark.asyncio
    async def test_defers_on_lock_contention(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        drain = _make_drain()
        row_id = await persist_batch(drain=drain, tenant_id=1, cipher=cipher, store=fake_store)

        # Pre-acquire the lock to simulate contention
        await fake_redis.set("lock:1", "other-worker", ex=120, nx=True)

        with pytest.raises(BatchLockContentionError):
            await process_batch(
                tenant_id=1,
                batch_id=row_id,
                redis=fake_redis,
                store=fake_store,
            )

        # Batch status unchanged
        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "pending"

    @pytest.mark.asyncio
    async def test_skips_failed_batch(
        self,
        cipher: ColumnCipher,
        fake_redis: FakeRedis,
        fake_store: FakeBatchStore,
    ) -> None:
        row_id = await persist_batch(
            drain=_make_drain(), tenant_id=1, cipher=cipher, store=fake_store
        )
        fake_store._rows[row_id].status = "failed"

        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,
            store=fake_store,
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "failed"
        assert row.attempts == 0

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
            redis=fake_redis,
            store=fake_store,
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
            redis=fake_redis,
            store=fake_store,
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
        assert fake_enqueuer.calls == [(1, row_id, 0)]

        # Step 3: process batch
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,
            store=fake_store,
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"
        assert row.attempts == 1

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
            redis=fake_redis,
            store=fake_store,
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"
        assert row.attempts == 1
        # Retry — should be idempotent
        await process_batch(
            tenant_id=1,
            batch_id=row_id,
            redis=fake_redis,
            store=fake_store,
        )

        row = await fake_store.get(batch_id=row_id, tenant_id=1)
        assert row is not None
        assert row.status == "done"
        assert row.attempts == 1  # retry skipped the terminal batch


@pytest.mark.asyncio
async def test_worker_uses_arq_retry_for_lock_contention(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeBatchStore,
) -> None:
    async def contended(**_: object) -> None:
        raise BatchLockContentionError

    monkeypatch.setattr(worker_module, "_process_batch", contended)
    ctx = {
        "redis": cast(ArqRedis, object()),
        "batch_store": fake_store,
    }

    with pytest.raises(Retry) as caught:
        await worker_module.process_batch(ctx, tenant_id=1, batch_id=42)

    assert caught.value.defer_score == 5_000


@pytest.mark.asyncio
async def test_worker_retries_processing_failures_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeBatchStore,
) -> None:
    async def unavailable(**_: object) -> None:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(worker_module, "_process_batch", unavailable)
    ctx = {
        "redis": cast(ArqRedis, object()),
        "batch_store": fake_store,
        "job_try": 2,
    }

    with pytest.raises(Retry) as caught:
        await worker_module.process_batch(ctx, tenant_id=1, batch_id=42)

    assert caught.value.defer_score == 2_000


@pytest.mark.asyncio
async def test_worker_startup_injects_postgres_batch_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = cast(AsyncEngine, object())
    sessions = cast(async_sessionmaker[AsyncSession], object())
    settings = cast(Settings, object())
    startup_calls: list[str] = []

    def validated_startup(service: str) -> tuple[Settings, object]:
        startup_calls.append(service)
        return settings, object()

    monkeypatch.setattr(worker_module, "startup", validated_startup)
    monkeypatch.setattr(
        worker_module,
        "get_engine",
        lambda received: engine if received is settings else pytest.fail("wrong settings"),
    )
    monkeypatch.setattr(worker_module, "session_factory", lambda _: sessions)
    ctx: dict[str, object] = {}

    await worker_module.worker_startup(ctx)

    assert startup_calls == ["worker"]
    assert ctx["db_engine"] is engine
    store = ctx["batch_store"]
    assert isinstance(store, PostgresBatchStore)
    assert store._sessions is sessions


def test_arq_redis_settings_use_validated_url_and_ca() -> None:
    settings = cast(
        Settings,
        SimpleNamespace(
            redis_url=SecretStr("rediss://:secret@redis.internal:6380/3"),
            fittrack_tls_ca_file="/certs/ca.crt",
        ),
    )

    redis = worker_module.build_redis_settings(settings)

    assert redis.host == "redis.internal"
    assert redis.port == 6380
    assert redis.database == 3
    assert redis.password == "secret"
    assert redis.ssl is True
    assert redis.ssl_ca_certs == "/certs/ca.crt"
    assert redis.ssl_check_hostname is True


def test_worker_settings_register_batch_retry_policy() -> None:
    registered = {function.name: function for function in worker_module.WorkerSettings.functions}

    assert registered["process_batch"].max_tries == 3
    assert registered["process_batch"].keep_result_s == 0
    assert worker_module.WorkerSettings.on_startup is worker_module.worker_startup
    assert worker_module.WorkerSettings.redis_settings.ssl is True
    assert worker_module.WorkerSettings.redis_settings.ssl_check_hostname is True


@pytest.mark.asyncio
async def test_postgres_batch_store_round_trip(
    app_dsn: str,
    migrated: None,
    owner: asyncpg.Connection,
    cipher: ColumnCipher,
) -> None:
    tenant_id: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ('batch-store') RETURNING id"
    )
    url, ssl_args = split_ssl_arguments(
        app_dsn.replace("postgresql://", "postgresql+asyncpg://")
        + f"?sslmode=verify-full&sslrootcert={Path(CA_FILE)}"
    )
    engine = create_async_engine(url, connect_args=ssl_args)
    store = PostgresBatchStore(session_factory(engine))
    try:
        row_id = await persist_batch(
            drain=_make_drain(),
            tenant_id=tenant_id,
            cipher=cipher,
            store=store,
        )

        row = await store.get(batch_id=row_id, tenant_id=tenant_id)
        assert row is not None
        assert row.message_ids == ["101", "102"]
        assert row.status == "pending"
        assert (
            await store.find_by_message_ids(tenant_id=tenant_id, message_ids=["101", "102"]) == row
        )

        await store.mark_done(batch_id=row_id, tenant_id=tenant_id)
        completed = await store.get(batch_id=row_id, tenant_id=tenant_id)
        assert completed is not None
        assert completed.status == "done"
        assert completed.attempts == 1
    finally:
        await engine.dispose()
