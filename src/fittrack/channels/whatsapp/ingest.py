"""What happens to a delivery once it is authenticated.

Kept out of the request handler so the response path stays short: the handler
validates and acknowledges, this runs afterwards and decides what to persist
and enqueue.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fittrack.channels.whatsapp.payload import InboundMessage, StatusUpdate
from fittrack.crypto.aesgcm import Encryptor

log = logging.getLogger(__name__)


class Buffer(Protocol):
    async def push(self, bsuid: str, message: dict[str, Any]) -> None: ...


class Scheduler(Protocol):
    """The ARQ pool, narrowed to what ingress needs."""

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any: ...


# Margin on top of the debounce window. The job must fire after the deadline
# it was scheduled against, and ARQ's poll interval plus clock skew can land it
# a hair early -- which costs a whole extra round trip through the queue.
SCHEDULE_MARGIN_SECONDS: Final = 2


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

    def __init__(
        self,
        engine: AsyncEngine,
        encryptor: Encryptor,
        buffer: Buffer,
        scheduler: Scheduler,
        window_seconds: int,
    ) -> None:
        self._engine = engine
        self._encryptor = encryptor
        self._buffer = buffer
        self._scheduler = scheduler
        self._window = window_seconds

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
            await self._schedule_flush(message.bsuid)

    async def _schedule_flush(self, bsuid: str) -> None:
        """Asks a worker to look at this user once the window has closed.

        Buffering without scheduling is where the burst dies: nothing else in
        the system enqueues `flush_user`, so the messages would sit in Redis
        until their TTL and the user would never get an answer.

        One job per message rather than one per burst. The window renews on
        every message, so an early job finds the buffer not ready and returns;
        the job belonging to the last message is the one that fires after the
        real deadline. Deduplicating on the user would mean pinning the flush
        to the *first* message's deadline and cutting the burst short.
        """
        try:
            await self._scheduler.enqueue_job(
                "flush_user", bsuid, _defer_by=self._window + SCHEDULE_MARGIN_SECONDS
            )
        except Exception:
            # The delivery is already persisted and buffered, so this is
            # recoverable: the next message from this user schedules a flush
            # that picks up everything. Failing the request would make Meta
            # redeliver a message we already stored.
            log.exception("could not schedule the flush for %s", bsuid)

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
