"""Real PostgreSQL persistence for outbound delivery and encrypted payloads."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fittrack.channels.base import OutboundBlock, SendReceipt
from fittrack.db.engine import split_ssl_arguments, tenant_session
from fittrack.security.crypto import ColumnCipher, DecryptionError, Keyring
from fittrack.services.outbound import (
    NewOutbound,
    PostgresOutboundQueueStore,
    decode_item_payload,
)
from tests.conftest import CA_FILE

NOW = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)


@pytest.fixture
def cipher() -> ColumnCipher:
    return ColumnCipher(Keyring(keys={1: b"\x66" * 32}, active_version=1))


@pytest.fixture
async def sessions(app_dsn: str, migrated: None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url, ssl_args = split_ssl_arguments(
        app_dsn.replace("postgresql://", "postgresql+asyncpg://")
        + f"?sslmode=verify-full&sslrootcert={CA_FILE}"
    )
    engine = create_async_engine(url, connect_args=ssl_args)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def make_destination(owner: asyncpg.Connection) -> tuple[int, int]:
    tenant_id: int = await owner.fetchval("INSERT INTO tenant DEFAULT VALUES RETURNING id")
    identity_id: int = await owner.fetchval(
        "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash) "
        "VALUES ($1, 'telegram', $2, $3) RETURNING id",
        tenant_id,
        b"sealed-outbound-identity",
        secrets.token_bytes(32),
    )
    return tenant_id, identity_id


async def test_outbound_queue_persists_encrypted_group_lifecycle_and_rls(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    tenant_id, identity_id = await make_destination(owner)
    other_tenant_id: int = await owner.fetchval("INSERT INTO tenant DEFAULT VALUES RETURNING id")
    group_id = uuid4()
    blocks = [
        OutboundBlock(kind="text", text="first private block"),
        OutboundBlock(kind="text", text="second private block"),
        OutboundBlock(kind="text", text="third private block"),
    ]
    store = PostgresOutboundQueueStore(sessions, cipher)
    await store.enqueue(
        [
            NewOutbound(
                tenant_id=tenant_id,
                identity_id=identity_id,
                channel="telegram",
                block=block,
                group_id=group_id,
                seq=seq,
                scheduled_at=NOW,
                proactive=seq == 2,
            )
            for seq, block in enumerate(blocks)
        ]
    )

    rows = await owner.fetch(
        "SELECT id, tenant_id, payload, key_version, group_id, seq, attempts, retryable, "
        "next_retry_at, sent_at, dead_at, error_code "
        "FROM outbound_queue WHERE group_id = $1 ORDER BY seq",
        group_id,
    )
    assert len(rows) == 3
    assert [row["seq"] for row in rows] == [0, 1, 2]
    assert {row["group_id"] for row in rows} == {group_id}

    for seq, row in enumerate(rows):
        block_text = blocks[seq].text
        assert block_text is not None
        assert block_text.encode() not in bytes(row["payload"])
        decoded, proactive = decode_item_payload(
            item_id=row["id"],
            tenant_id=tenant_id,
            payload=bytes(row["payload"]),
            key_version=row["key_version"],
            cipher=cipher,
        )
        assert decoded == blocks[seq]
        assert proactive is (seq == 2)

    with pytest.raises(DecryptionError):
        decode_item_payload(
            item_id=rows[1]["id"],
            tenant_id=tenant_id,
            payload=bytes(rows[0]["payload"]),
            key_version=rows[0]["key_version"],
            cipher=cipher,
        )

    async with tenant_session(sessions, tenant_id) as session:
        own_count = await session.scalar(
            text("SELECT count(*) FROM outbound_queue WHERE group_id = :group_id"),
            {"group_id": group_id},
        )
    async with tenant_session(sessions, other_tenant_id) as session:
        other_count = await session.scalar(
            text("SELECT count(*) FROM outbound_queue WHERE group_id = :group_id"),
            {"group_id": group_id},
        )
    assert own_count == 3
    assert other_count == 0

    retry_at = NOW + timedelta(seconds=32)
    await store.mark_retry(
        item_id=rows[1]["id"],
        tenant_id=tenant_id,
        attempts=4,
        next_retry_at=retry_at,
        error_code="503",
    )
    await store.mark_sent(
        item_id=rows[0]["id"],
        tenant_id=tenant_id,
        attempts=1,
        receipt=SendReceipt(
            channel="telegram",
            channel_message_id="sent-1",
            sent_at=NOW,
        ),
    )
    dead_at = NOW + timedelta(minutes=1)
    await store.mark_dead(
        item_id=rows[1]["id"],
        tenant_id=tenant_id,
        group_id=UUID(str(group_id)),
        seq=1,
        attempts=5,
        error_code="400",
        dead_at=dead_at,
    )

    persisted = await owner.fetch(
        "SELECT seq, attempts, retryable, next_retry_at, sent_at, dead_at, error_code "
        "FROM outbound_queue WHERE group_id = $1 ORDER BY seq",
        group_id,
    )
    assert dict(persisted[0]) == {
        "seq": 0,
        "attempts": 1,
        "retryable": None,
        "next_retry_at": NOW,
        "sent_at": NOW,
        "dead_at": None,
        "error_code": None,
    }
    assert dict(persisted[1]) == {
        "seq": 1,
        "attempts": 5,
        "retryable": False,
        "next_retry_at": retry_at,
        "sent_at": None,
        "dead_at": dead_at,
        "error_code": "400",
    }
    assert dict(persisted[2]) == {
        "seq": 2,
        "attempts": 0,
        "retryable": False,
        "next_retry_at": NOW,
        "sent_at": None,
        "dead_at": dead_at,
        "error_code": "400",
    }


async def test_outbound_store_revokes_only_the_tenant_scoped_identity(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    tenant_id, identity_id = await make_destination(owner)
    other_tenant_id: int = await owner.fetchval("INSERT INTO tenant DEFAULT VALUES RETURNING id")
    await owner.execute(
        "UPDATE channel_identity SET is_primary = true WHERE id = $1",
        identity_id,
    )
    store = PostgresOutboundQueueStore(sessions, cipher)

    with pytest.raises(LookupError, match="identity does not belong to tenant"):
        await store.revoke_identity(
            identity_id=identity_id,
            tenant_id=other_tenant_id,
            revoked_at=NOW,
        )

    still_live = await owner.fetchrow(
        "SELECT revoked_at, is_primary FROM channel_identity WHERE id = $1", identity_id
    )
    assert still_live["revoked_at"] is None
    assert still_live["is_primary"] is True

    await store.revoke_identity(
        identity_id=identity_id,
        tenant_id=tenant_id,
        revoked_at=NOW,
    )
    revoked = await owner.fetchrow(
        "SELECT revoked_at, is_primary FROM channel_identity WHERE id = $1", identity_id
    )
    assert revoked["revoked_at"] == NOW
    assert revoked["is_primary"] is False


async def test_outbound_store_refuses_to_persist_a_worker_local_media_path(
    tmp_path: Path,
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    tenant_id, identity_id = await make_destination(owner)
    media_path = tmp_path / "worker-local-photo.jpg"
    media_path.write_bytes(b"private image")
    group_id = uuid4()
    store = PostgresOutboundQueueStore(sessions, cipher)

    with pytest.raises(ValueError, match="durable shared media storage"):
        await store.enqueue(
            [
                NewOutbound(
                    tenant_id=tenant_id,
                    identity_id=identity_id,
                    channel="telegram",
                    block=OutboundBlock(kind="media", media_path=media_path),
                    group_id=group_id,
                    seq=0,
                    scheduled_at=NOW,
                )
            ]
        )

    assert (
        await owner.fetchval("SELECT count(*) FROM outbound_queue WHERE group_id = $1", group_id)
        == 0
    )
