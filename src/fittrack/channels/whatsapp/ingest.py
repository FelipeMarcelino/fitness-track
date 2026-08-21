"""What happens to a delivery once it is authenticated.

Kept out of the request handler so the response path stays short: the handler
validates and hands over, this decides what to persist and enqueue.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fittrack.channels.whatsapp.payload import InboundMessage, StatusUpdate
from fittrack.crypto.aesgcm import Encryptor

log = logging.getLogger(__name__)


class Deduplicator(Protocol):
    async def seen(self, message_id: str) -> bool: ...


class Buffer(Protocol):
    async def push(self, bsuid: str, message: dict[str, Any]) -> None: ...


class Ingest:
    """Persists the raw delivery and pushes it onto the burst buffer.

    Order matters and is not obvious: the tenant is upserted before
    raw_message, because raw_message.tenant_id is NOT NULL ON DELETE CASCADE
    (§5.2) so that an erasure request cannot leave orphaned message bodies
    behind.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        encryptor: Encryptor,
        dedup: Deduplicator,
        buffer: Buffer,
    ) -> None:
        self._engine = engine
        self._encryptor = encryptor
        self._dedup = dedup
        self._buffer = buffer

    async def accept_message(self, message: InboundMessage, envelope: dict[str, Any]) -> None:
        if await self._dedup.seen(message.message_id):
            # Meta redelivers anything it did not get a 200 for, and a redelivery
            # of a set already recorded would double the workout volume.
            log.info("ignoring redelivery of %s", message.message_id)
            return

        tenant_id = await self._store(message, envelope)
        if message.is_actionable:
            await self._buffer.push(
                message.bsuid,
                {
                    "message_id": message.message_id,
                    "tenant_id": tenant_id,
                    "type": message.msg_type,
                    "text": message.text,
                    "media_id": message.media_id,
                    "button_id": message.button_id,
                },
            )

    async def accept_status(self, update: StatusUpdate) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE outbound_queue SET error_code = :code "
                    "WHERE payload->>'wa_message_id' = :id AND :code IS NOT NULL"
                ),
                {"id": update.message_id, "code": update.error_code},
            )

    async def _store(self, message: InboundMessage, envelope: dict[str, Any]) -> int:
        payload_blob, key_version = self._encryptor.encrypt_json(envelope)

        async with self._engine.begin() as conn:
            row = await conn.execute(
                text(
                    "INSERT INTO tenant (bsuid) VALUES (:b) "
                    "ON CONFLICT (bsuid) WHERE deleted_at IS NULL DO UPDATE "
                    "SET updated_at = now() RETURNING id"
                ),
                {"b": message.bsuid},
            )
            tenant_id = int(row.scalar_one())

            await conn.execute(
                text(
                    "INSERT INTO raw_message "
                    "(tenant_id, wa_message_id, direction, msg_type, payload, key_version) "
                    "VALUES (:t, :m, 'inbound', :k, :p, :v) "
                    "ON CONFLICT (wa_message_id) DO NOTHING"
                ),
                {
                    "t": tenant_id,
                    "m": message.message_id,
                    "k": message.msg_type,
                    "p": payload_blob,
                    "v": key_version,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO conversation_window (tenant_id, last_inbound_at) "
                    "VALUES (:t, now()) ON CONFLICT (tenant_id) DO UPDATE "
                    "SET last_inbound_at = now()"
                ),
                {"t": tenant_id},
            )
        return tenant_id


def envelope_json(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, sort_keys=True, ensure_ascii=False)
