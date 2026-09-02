"""Durable outbound delivery, retry policy, and shared rate limiting.

The channel adapter translates provider failures into :class:`ClassifiedError`;
this module decides whether and when the same ``outbound_queue`` row may run
again.  Provider error codes do not cross this boundary in the other direction.

All coordination is durable. Queue state is in PostgreSQL and send pacing is in
Redis, so any of the four stateless workers can continue another worker's work
(spec 17.4, 18.2, 18.4).
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fittrack.channels.base import (
    ChannelIdentity,
    ClassifiedError,
    ErrorClass,
    OutboundBlock,
    SendReceipt,
    TemplateRef,
)
from fittrack.db.engine import tenant_session
from fittrack.security.crypto import ColumnCipher, column_aad
from fittrack.settings import ChannelKind

BACKOFF_LADDER_SECONDS = (2, 8, 32, 2 * 60, 8 * 60)
MAX_RETRIES = len(BACKOFF_LADDER_SECONDS)
MAX_JITTER = 0.25

DEFAULT_UNSUPPORTED_MEDIA_PROMPT = (
    Path(__file__).resolve().parents[3] / "config" / "prompts" / "unsupported_media.md"
)


class DeliveryStatus(StrEnum):
    """The durable outcome of one send attempt."""

    SENT = "sent"
    RETRY = "retry"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What the caller needs to decide whether there is more work now."""

    status: DeliveryStatus
    attempts: int
    next_retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewOutbound:
    """One block before the queue store assigns and encrypts its row id."""

    tenant_id: int
    identity_id: int
    channel: ChannelKind
    block: OutboundBlock
    group_id: UUID
    seq: int
    scheduled_at: datetime
    proactive: bool = False


@dataclass(frozen=True, slots=True)
class OutboundItem:
    """A decrypted queue row ready for one delivery attempt."""

    id: int
    tenant_id: int
    identity_id: int
    channel: ChannelKind
    block: OutboundBlock
    group_id: UUID
    seq: int
    attempts: int
    proactive: bool = False


class OutboundQueueStore(Protocol):
    """The PostgreSQL mutations used by the delivery service."""

    async def enqueue(self, rows: Sequence[NewOutbound]) -> None: ...

    async def mark_sent(self, *, item_id: int, tenant_id: int, receipt: SendReceipt) -> None: ...

    async def mark_retry(
        self,
        *,
        item_id: int,
        tenant_id: int,
        attempts: int,
        next_retry_at: datetime,
        error_code: str | None,
    ) -> None: ...

    async def mark_dead(
        self,
        *,
        item_id: int,
        tenant_id: int,
        group_id: UUID,
        seq: int,
        attempts: int,
        error_code: str,
        dead_at: datetime,
    ) -> None: ...


class OutboundRateLimiter(Protocol):
    """A send slot shared by every worker process."""

    async def acquire(self, identity_id: int) -> None: ...


class OutboundChannel(Protocol):
    """The outbound subset of the channel contract."""

    kind: ClassVar[ChannelKind]

    async def send(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt: ...

    def classify_error(self, exc: Exception) -> ClassifiedError: ...


Jitter = Callable[[], float]
Clock = Callable[[], datetime]
GroupIdFactory = Callable[[], UUID]


def retry_delay(
    verdict: ClassifiedError,
    retry_number: int,
    *,
    proactive: bool = False,
    jitter: Jitter = lambda: random.uniform(-MAX_JITTER, MAX_JITTER),
) -> timedelta | None:
    """Return the delay after a failed send, or ``None`` for a dead letter.

    ``retry_number`` is one-based and counts scheduled retries, not the initial
    send. Consequently all five rungs in the specified ladder are reachable.
    The sixth failure after those retries becomes dead.
    """
    if retry_number <= 0:
        raise ValueError("retry_number must be positive")
    if proactive or retry_number > MAX_RETRIES:
        return None
    if verdict.error_class is ErrorClass.RETRY_AFTER:
        # ClassifiedError guarantees this is present and non-negative.
        assert verdict.retry_after is not None
        return timedelta(seconds=verdict.retry_after)
    if verdict.error_class is not ErrorClass.RETRY_BACKOFF:
        return None

    jitter_fraction = jitter()
    if not -MAX_JITTER <= jitter_fraction <= MAX_JITTER:
        raise ValueError("jitter must be between -0.25 and 0.25")
    base = BACKOFF_LADDER_SECONDS[retry_number - 1]
    return timedelta(seconds=base * (1 + jitter_fraction))


def unsupported_media_block(
    prompt_path: Path = DEFAULT_UNSUPPORTED_MEDIA_PROMPT,
) -> OutboundBlock:
    """Load the fixed photo/document response from versioned configuration."""
    text_content = prompt_path.read_text(encoding="utf-8").strip()
    if not text_content:
        raise ValueError(f"{prompt_path.name}: unsupported-media prompt is empty")
    return OutboundBlock(kind="text", text=text_content)


class RedisRateLimitClient(Protocol):
    """The single Redis command needed for atomic, cross-worker pacing."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> int: ...


_ACQUIRE_RATE_SLOT = """\
-- KEYS[1] = outbound:rate:global
-- KEYS[2] = outbound:rate:identity:{identity_id}
-- ARGV[1] = unique reservation token
-- ARGV[2] = global sliding-window size in milliseconds
-- ARGV[3] = global limit within that window
-- ARGV[4] = minimum interval per identity in milliseconds
local redis_time = redis.call('TIME')
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local global_window = tonumber(ARGV[2])
local global_limit = tonumber(ARGV[3])
local chat_interval = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - global_window)

local wait_ms = 0
local global_count = redis.call('ZCARD', KEYS[1])
if global_count >= global_limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  wait_ms = math.max(wait_ms, tonumber(oldest[2]) + global_window - now_ms)
end

local last_chat_send = redis.call('GET', KEYS[2])
if last_chat_send then
  wait_ms = math.max(wait_ms, tonumber(last_chat_send) + chat_interval - now_ms)
end

if wait_ms > 0 then
  return math.ceil(wait_ms)
end

redis.call('ZADD', KEYS[1], now_ms, ARGV[1])
redis.call('PEXPIRE', KEYS[1], global_window * 2)
redis.call('SET', KEYS[2], now_ms, 'PX', chat_interval * 2)
return 0
"""


class RedisRateLimiter:
    """A Redis-coordinated 30/s global and one-second per-chat limiter.

    The Lua script uses Redis's clock and reserves both scopes atomically. Two
    limiter objects in different worker processes therefore still observe the
    same slots; no process-local semaphore participates in the decision.
    """

    GLOBAL_LIMIT = 30
    GLOBAL_WINDOW_MS = 1_000
    CHAT_INTERVAL_MS = 1_000
    GLOBAL_KEY = "outbound:rate:global"

    def __init__(
        self,
        redis: RedisRateLimitClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        token: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._redis = redis
        self._sleep = sleep
        self._token = token

    async def acquire(self, identity_id: int) -> None:
        if identity_id <= 0:
            raise ValueError("identity_id must be positive")
        chat_key = f"outbound:rate:identity:{identity_id}"
        while True:
            wait_ms = int(
                await self._redis.eval(
                    _ACQUIRE_RATE_SLOT,
                    2,
                    self.GLOBAL_KEY,
                    chat_key,
                    self._token(),
                    self.GLOBAL_WINDOW_MS,
                    self.GLOBAL_LIMIT,
                    self.CHAT_INTERVAL_MS,
                )
            )
            if wait_ms < 0:
                raise RuntimeError("Redis rate limiter returned a negative wait")
            if wait_ms == 0:
                return
            await self._sleep(wait_ms / 1_000)


class OutboundService:
    """Queue response blocks and deliver them through the single output path."""

    def __init__(
        self,
        *,
        store: OutboundQueueStore,
        rate_limiter: OutboundRateLimiter,
        now: Clock = lambda: datetime.now(UTC),
        jitter: Jitter = lambda: random.uniform(-MAX_JITTER, MAX_JITTER),
        group_id: GroupIdFactory = uuid.uuid4,
    ) -> None:
        self._store = store
        self._rate_limiter = rate_limiter
        self._now = now
        self._jitter = jitter
        self._group_id = group_id

    async def enqueue_response(
        self,
        *,
        tenant_id: int,
        identity_id: int,
        channel: ChannelKind,
        blocks: Sequence[OutboundBlock],
        proactive: bool = False,
        scheduled_at: datetime | None = None,
    ) -> UUID:
        """Persist one answer with a fresh group, even when it has one block."""
        if tenant_id <= 0 or identity_id <= 0:
            raise ValueError("tenant_id and identity_id must be positive")
        if not blocks:
            raise ValueError("an outbound response must contain at least one block")
        group_id = self._group_id()
        schedule = scheduled_at or self._now()
        if schedule.tzinfo is None or schedule.utcoffset() is None:
            raise ValueError("scheduled_at must be timezone-aware")
        rows = [
            NewOutbound(
                tenant_id=tenant_id,
                identity_id=identity_id,
                channel=channel,
                block=block,
                group_id=group_id,
                seq=seq,
                scheduled_at=schedule,
                proactive=proactive,
            )
            for seq, block in enumerate(blocks)
        ]
        await self._store.enqueue(rows)
        return group_id

    async def enqueue_unsupported_media(
        self,
        *,
        tenant_id: int,
        identity_id: int,
        channel: ChannelKind,
        media_kind: Literal["image", "document"],
        prompt_path: Path = DEFAULT_UNSUPPORTED_MEDIA_PROMPT,
    ) -> UUID:
        """Queue the AD-27 pt-BR photo/document degradation as a normal response."""
        # The parameter makes both accepted inbound cases explicit at the call
        # site. They deliberately share one response in phase 1.0.
        if media_kind not in ("image", "document"):
            raise ValueError("unsupported-media degradation only handles image or document")
        return await self.enqueue_response(
            tenant_id=tenant_id,
            identity_id=identity_id,
            channel=channel,
            blocks=[unsupported_media_block(prompt_path)],
        )

    async def deliver(
        self,
        *,
        item: OutboundItem,
        identity: ChannelIdentity,
        channel: OutboundChannel,
    ) -> DeliveryResult:
        """Attempt one row, persisting success, retry time, or dead status."""
        _require_matching_destination(item, identity, channel)
        await self._rate_limiter.acquire(item.identity_id)
        try:
            receipt = await channel.send(identity, item.block)
        except Exception as error:
            verdict = channel.classify_error(error)
            attempts = item.attempts + 1
            delay = retry_delay(
                verdict,
                attempts,
                proactive=item.proactive,
                jitter=self._jitter,
            )
            if delay is not None:
                next_retry_at = self._now() + delay
                await self._store.mark_retry(
                    item_id=item.id,
                    tenant_id=item.tenant_id,
                    attempts=attempts,
                    next_retry_at=next_retry_at,
                    error_code=verdict.code,
                )
                return DeliveryResult(DeliveryStatus.RETRY, attempts, next_retry_at)

            error_code = verdict.code or verdict.error_class.value
            await self._store.mark_dead(
                item_id=item.id,
                tenant_id=item.tenant_id,
                group_id=item.group_id,
                seq=item.seq,
                attempts=attempts,
                error_code=error_code,
                dead_at=self._now(),
            )
            return DeliveryResult(DeliveryStatus.DEAD, attempts)

        await self._store.mark_sent(
            item_id=item.id,
            tenant_id=item.tenant_id,
            receipt=receipt,
        )
        return DeliveryResult(DeliveryStatus.SENT, item.attempts)


def _require_matching_destination(
    item: OutboundItem,
    identity: ChannelIdentity,
    channel: OutboundChannel,
) -> None:
    if (
        identity.tenant_id != item.tenant_id
        or identity.identity_id != item.identity_id
        or identity.channel != item.channel
        or channel.kind != item.channel
    ):
        raise ValueError("outbound item, identity, and channel must name one destination")


class PostgresOutboundQueueStore:
    """Encrypted ``outbound_queue`` persistence using its existing schema."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cipher: ColumnCipher,
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher

    async def enqueue(self, rows: Sequence[NewOutbound]) -> None:
        if not rows:
            return
        tenant_id = rows[0].tenant_id
        if any(row.tenant_id != tenant_id for row in rows):
            raise ValueError("one enqueue call cannot cross tenant boundaries")
        ids = await self._reserve_ids(len(rows))
        async with tenant_session(self._sessions, tenant_id) as session:
            for item_id, row in zip(ids, rows, strict=True):
                payload = self._cipher.encrypt(
                    _encode_payload(row.block, proactive=row.proactive),
                    column_aad(
                        tenant_id=tenant_id,
                        table="outbound_queue",
                        column="payload",
                        row_id=item_id,
                    ),
                )
                await session.execute(
                    text(
                        "INSERT INTO outbound_queue ("
                        "id, tenant_id, identity_id, channel, kind, payload, key_version, "
                        "group_id, seq, scheduled_at, next_retry_at"
                        ") VALUES ("
                        ":id, :tenant_id, :identity_id, CAST(:channel AS channel_kind), "
                        ":kind, :payload, :key_version, :group_id, :seq, "
                        ":scheduled_at, :scheduled_at"
                        ")"
                    ),
                    {
                        "id": item_id,
                        "tenant_id": row.tenant_id,
                        "identity_id": row.identity_id,
                        "channel": row.channel,
                        "kind": row.block.kind,
                        "payload": payload,
                        "key_version": self._cipher.active_version,
                        "group_id": row.group_id,
                        "seq": row.seq,
                        "scheduled_at": row.scheduled_at,
                    },
                )

    async def _reserve_ids(self, count: int) -> list[int]:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                text(
                    "SELECT nextval('outbound_queue_id_seq') AS id FROM generate_series(1, :count)"
                ),
                {"count": count},
            )
            return [int(row.id) for row in result]

    async def mark_sent(self, *, item_id: int, tenant_id: int, receipt: SendReceipt) -> None:
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE outbound_queue SET sent_at = :sent_at, retryable = NULL, "
                    "error_code = NULL, last_error = NULL "
                    "WHERE id = :id"
                ),
                {"id": item_id, "sent_at": receipt.sent_at},
            )

    async def mark_retry(
        self,
        *,
        item_id: int,
        tenant_id: int,
        attempts: int,
        next_retry_at: datetime,
        error_code: str | None,
    ) -> None:
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE outbound_queue SET attempts = :attempts, retryable = true, "
                    "next_retry_at = :next_retry_at, error_code = :error_code, "
                    "last_error = NULL WHERE id = :id AND sent_at IS NULL AND dead_at IS NULL"
                ),
                {
                    "id": item_id,
                    "attempts": attempts,
                    "next_retry_at": next_retry_at,
                    "error_code": error_code,
                },
            )

    async def mark_dead(
        self,
        *,
        item_id: int,
        tenant_id: int,
        group_id: UUID,
        seq: int,
        attempts: int,
        error_code: str,
        dead_at: datetime,
    ) -> None:
        """Dead-letter this block and every unsent suffix block in its group."""
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE outbound_queue SET "
                    "attempts = CASE WHEN id = :id THEN :attempts ELSE attempts END, "
                    "retryable = false, error_code = :error_code, last_error = NULL, "
                    "dead_at = :dead_at "
                    "WHERE group_id = :group_id AND seq >= :seq "
                    "AND sent_at IS NULL AND dead_at IS NULL"
                ),
                {
                    "id": item_id,
                    "attempts": attempts,
                    "error_code": error_code,
                    "dead_at": dead_at,
                    "group_id": group_id,
                    "seq": seq,
                },
            )


def _encode_payload(block: OutboundBlock, *, proactive: bool) -> bytes:
    template = None
    if block.template is not None:
        template = {
            "name": block.template.name,
            "language": block.template.language,
            "parameters": list(block.template.parameters),
        }
    payload = {
        "kind": block.kind,
        "text": block.text,
        "emoji": block.emoji,
        "buttons": list(block.buttons) if block.buttons is not None else None,
        "media_path": str(block.media_path) if block.media_path is not None else None,
        "reply_to": list(block.reply_to) if block.reply_to is not None else None,
        "template": template,
        "proactive": proactive,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def decode_item_payload(
    *,
    item_id: int,
    tenant_id: int,
    payload: bytes,
    key_version: int,
    cipher: ColumnCipher,
) -> tuple[OutboundBlock, bool]:
    """Decrypt one queue payload without exposing its user-facing content."""
    plaintext = cipher.decrypt(
        payload,
        column_aad(
            tenant_id=tenant_id,
            table="outbound_queue",
            column="payload",
            row_id=item_id,
        ),
        key_version,
    )
    raw = json.loads(plaintext)
    if not isinstance(raw, dict):
        raise ValueError("outbound payload must be an object")
    reply_to = raw.get("reply_to")
    template_raw = raw.get("template")
    template = None
    if isinstance(template_raw, dict):
        template = TemplateRef(
            name=str(template_raw["name"]),
            language=str(template_raw["language"]),
            parameters=tuple(str(value) for value in template_raw.get("parameters", [])),
        )
    block = OutboundBlock(
        kind=raw["kind"],
        text=raw.get("text"),
        emoji=raw.get("emoji"),
        buttons=tuple(raw["buttons"]) if raw.get("buttons") is not None else None,
        media_path=Path(raw["media_path"]) if raw.get("media_path") is not None else None,
        reply_to=(reply_to[0], str(reply_to[1])) if reply_to is not None else None,
        template=template,
    )
    return block, raw.get("proactive") is True
