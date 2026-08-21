"""Batch durability and retry accounting (§4.1, §17.4)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fittrack.services.batch import MAX_ATTEMPTS, BatchStore

pytestmark = pytest.mark.integration


@pytest.fixture
def store(app_dsn: str) -> BatchStore:
    """The application role, so RLS is live -- the same trap that made the
    ingest suite pass while production writes were rejected."""
    return BatchStore(create_async_engine(app_dsn))


async def _tenant(conn: AsyncConnection, bsuid: str) -> int:
    row = await conn.execute(
        text("INSERT INTO tenant (bsuid) VALUES (:b) RETURNING id"), {"b": bsuid}
    )
    await conn.commit()
    return int(row.scalar_one())


async def test_a_burst_is_combined_into_one_utterance(
    store: BatchStore, owner_conn: AsyncConnection
) -> None:
    """The extractor should read four fragments as one sentence, which is the
    entire reason the buffer exists."""
    tenant_id = await _tenant(owner_conn, "batch-1")

    batch = await store.create(
        tenant_id,
        [
            {"message_id": "m1", "text": "supino reto"},
            {"message_id": "m2", "text": "10kg"},
            {"message_id": "m3", "text": "8 reps"},
            {"message_id": "m4", "text": "foi facil"},
        ],
    )

    assert batch is not None
    assert batch.combined_text == "supino reto | 10kg | 8 reps | foi facil"
    assert batch.message_ids == ["m1", "m2", "m3", "m4"]


async def test_a_batch_survives_the_worker(store: BatchStore, owner_conn: AsyncConnection) -> None:
    """Redis no longer holds the messages -- the drain took them -- so if the
    batch were not persisted a crash here would lose the burst outright."""
    tenant_id = await _tenant(owner_conn, "batch-2")

    batch = await store.create(tenant_id, [{"message_id": "m1", "text": "supino"}])
    assert batch is not None

    row = await owner_conn.execute(
        text("SELECT combined_text, status FROM processing_batch WHERE id = :i"),
        {"i": batch.id},
    )
    combined, status = row.one()
    assert combined == "supino"
    assert status == "pending"


async def test_attempts_accumulate_and_then_exhaust(
    store: BatchStore, owner_conn: AsyncConnection
) -> None:
    tenant_id = await _tenant(owner_conn, "batch-3")
    batch = await store.create(tenant_id, [{"message_id": "m", "text": "x"}])
    assert batch is not None

    attempts = 0
    for _ in range(MAX_ATTEMPTS):
        attempts = await store.mark_attempt(batch)

    assert attempts == MAX_ATTEMPTS


async def test_a_failed_batch_is_recorded_with_its_error(
    store: BatchStore, owner_conn: AsyncConnection
) -> None:
    """Terminal failure must be visible. Silence here means a user whose
    workout vanished with nothing to point at."""
    tenant_id = await _tenant(owner_conn, "batch-4")
    batch = await store.create(tenant_id, [{"message_id": "m", "text": "x"}])
    assert batch is not None

    await store.mark_failed(batch, "provider unavailable")

    row = await owner_conn.execute(
        text("SELECT status, error, finished_at FROM processing_batch WHERE id = :i"),
        {"i": batch.id},
    )
    status, error, finished_at = row.one()
    assert status == "failed"
    assert error == "provider unavailable"
    assert finished_at is not None


async def test_an_empty_burst_creates_nothing(
    store: BatchStore, owner_conn: AsyncConnection
) -> None:
    """A flush racing another worker finds nothing. Creating a batch anyway
    would cost an LLM call to process no text at all."""
    tenant_id = await _tenant(owner_conn, "batch-5")

    assert await store.create(tenant_id, []) is None
