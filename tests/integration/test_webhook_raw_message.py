"""Real-database persistence for inbound webhook payloads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fittrack.channels.base import InboundMessage
from fittrack.db.engine import split_ssl_arguments
from fittrack.security.crypto import ColumnCipher, Keyring, column_aad
from fittrack.services.webhook import IngressIdentity, SqlRawMessageStore
from tests.conftest import CA_FILE


@pytest.fixture
def cipher() -> ColumnCipher:
    return ColumnCipher(Keyring(keys={1: b"\x55" * 32}, active_version=1))


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


async def make_identity(owner: asyncpg.Connection) -> IngressIdentity:
    tenant_id: int = await owner.fetchval("INSERT INTO tenant DEFAULT VALUES RETURNING id")
    digest = b"webhook-raw-message-hash"
    identity_id: int = await owner.fetchval(
        "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash) "
        "VALUES ($1, 'telegram', $2, $3) RETURNING id",
        tenant_id,
        b"sealed-by-fixture",
        digest,
    )
    return IngressIdentity(tenant_id, identity_id, digest)


def inbound() -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        external_id="private-chat-id",
        channel_message_id="message-204",
        kind="text",
        text="treino concluido",
        media_ref=None,
        button_payload=None,
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={"message": {"text": "treino concluido", "chat": {"id": "private-chat-id"}}},
    )


async def test_raw_message_is_encrypted_and_uses_the_second_dedup_barrier(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    identity = await make_identity(owner)
    store = SqlRawMessageStore(sessions=sessions, cipher=cipher)

    raw_message_id = await store.persist(identity=identity, message=inbound())
    duplicate = await store.persist(identity=identity, message=inbound())

    assert raw_message_id is not None
    assert duplicate == raw_message_id
    stored = await owner.fetchrow(
        "SELECT payload, key_version FROM raw_message WHERE id = $1", raw_message_id
    )
    aad = column_aad(
        tenant_id=identity.tenant_id,
        table="raw_message",
        column="payload",
        row_id=raw_message_id,
    )
    assert b"treino concluido" not in bytes(stored["payload"])
    assert cipher.decrypt(stored["payload"], aad, stored["key_version"]) == (
        b'{"message":{"chat":{"id":"private-chat-id"},"text":"treino concluido"}}'
    )
