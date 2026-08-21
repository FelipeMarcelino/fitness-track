"""What happens to a delivery once it is authenticated.

Kept out of the request handler so the response path stays short: the handler
validates and acknowledges, this runs afterwards and decides what to persist
and enqueue.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fittrack.channels.whatsapp.payload import InboundMessage, StatusUpdate
from fittrack.crypto.aesgcm import Encryptor

log = logging.getLogger(__name__)


class Buffer(Protocol):
    async def push(self, bsuid: str, message: dict[str, Any]) -> None: ...


class Ingest:
    """Persists the raw delivery and pushes it onto the burst buffer.

    Deduplication is the database's job, not a cache's. `raw_message` has a
    unique constraint on wa_message_id, so `ON CONFLICT DO NOTHING RETURNING id`
    tells us whether this delivery is new -- atomically, and only once the row
    is durable.

    An earlier version claimed the id in Redis before writing. If the write or
    the buffer push then failed, Meta's retry found the id already claimed and
    returned without persisting anything: a transient Postgres blip would have
    silently lost the user's workout.
    """

    def __init__(self, engine: AsyncEngine, encryptor: Encryptor, buffer: Buffer) -> None:
        self._engine = engine
        self._encryptor = encryptor
        self._buffer = buffer

    async def accept_message(self, message: InboundMessage, envelope: dict[str, Any]) -> None:
        stored = await self._store(message, envelope)
        if stored is None:
            log.info("ignoring redelivery of %s", message.message_id)
            return

        if message.is_actionable:
            await self._buffer.push(
                message.bsuid,
                {
                    "message_id": message.message_id,
                    "tenant_id": stored,
                    "type": message.msg_type,
                    "text": message.text,
                    "media_id": message.media_id,
                    "button_id": message.button_id,
                },
            )

    async def accept_status(self, update: StatusUpdate) -> None:
        if update.error_code is None:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE outbound_queue SET error_code = :code "
                    "WHERE payload->>'wa_message_id' = :id"
                ),
                {"id": update.message_id, "code": update.error_code},
            )

    async def _store(self, message: InboundMessage, envelope: dict[str, Any]) -> int | None:
        """Returns the tenant id, or None when this delivery was already stored.

        The tenant is upserted before raw_message because raw_message.tenant_id
        is NOT NULL ON DELETE CASCADE (§5.2), so an erasure cannot leave orphaned
        message bodies -- which means there is no such thing as a message
        without a tenant.
        """
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

            # Everything below is RLS-protected and forced, and the application
            # connects as fittrack_app, which is not a superuser. Without this
            # the policies reject the write and the delivery is lost.
            await conn.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))

            inserted = await conn.execute(
                text(
                    "INSERT INTO raw_message "
                    "(tenant_id, wa_message_id, direction, msg_type, payload, key_version) "
                    "VALUES (:t, :m, 'inbound', :k, :p, :v) "
                    "ON CONFLICT (wa_message_id) DO NOTHING RETURNING id"
                ),
                {
                    "t": tenant_id,
                    "m": message.message_id,
                    "k": message.msg_type,
                    "p": payload_blob,
                    "v": key_version,
                },
            )
            if inserted.scalar_one_or_none() is None:
                return None

            await conn.execute(
                text(
                    "INSERT INTO conversation_window (tenant_id, last_inbound_at) "
                    "VALUES (:t, now()) ON CONFLICT (tenant_id) DO UPDATE "
                    "SET last_inbound_at = now()"
                ),
                {"t": tenant_id},
            )
        return tenant_id
