"""Verified webhook ingestion without coupling it to a concrete channel.

The HTTP route owns status codes; this service owns the durable sequence after
authentication.  Its collaborators are small ports so the Telegram adapter
can be developed independently (Sprint 02 tasks S02-T02 and S02-T03).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fittrack.channels.base import InboundMessage
from fittrack.security.crypto import ColumnCipher
from fittrack.settings import ChannelKind

DEDUP_TTL_S = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class IngressIdentity:
    """The tenant-scoped identity needed after the pre-tenant boundary."""

    tenant_id: int
    identity_id: int
    external_id_hash: bytes


class UpdateDeduplicator(Protocol):
    """Reserves a Telegram update until all durable work has succeeded."""

    async def reserve(self, update_id: int) -> bool:
        """Return false when another delivery already completed or is running."""

    async def release(self, update_id: int) -> None:
        """Make a failed update eligible for Telegram redelivery."""


class InboundChannel(Protocol):
    """The inbound portion of the shared channel contract used by ingress."""

    kind: ClassVar[ChannelKind]

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        """Reject an unauthenticated request before it is parsed."""

    def parse(self, payload: Mapping[str, Any]) -> list[InboundMessage]:
        """Turn a verified update into its domain messages."""


class IngressIdentityResolver(Protocol):
    """The cached pre-tenant identity lookup and bootstrap boundary."""

    async def resolve_or_create(self, *, channel: ChannelKind, external_id: str) -> IngressIdentity:
        """Find or atomically bootstrap the channel identity."""


class IdentityCache(Protocol):
    """The small Redis surface used by the pre-tenant identity cache."""

    async def get(self, key: str) -> str | bytes | None:
        """Return the cached value when it has not expired."""

    async def set(self, key: str, value: str, *, ex: int) -> None:
        """Store a value with its mandatory expiry."""


class RawMessageStore(Protocol):
    """Persists encrypted inbound payloads under the resolved tenant context."""

    async def persist(self, *, identity: IngressIdentity, message: InboundMessage) -> int | None:
        """Return the raw-message id, or None for its SQL deduplication conflict."""


class TenantBuffer(Protocol):
    """Appends a processing envelope and renews its tenant debounce timer."""

    async def append(self, *, tenant_id: int, envelope: dict[str, object]) -> None:
        """Write an envelope that contains no plaintext external identifier."""


class RedisIngressClient(Protocol):
    """The Redis commands owned by webhook intake (not worker drain)."""

    async def delete(self, *names: str) -> int:
        """Delete one or more keys."""

    async def exists(self, *names: str) -> int:
        """Return how many of the requested keys currently exist."""

    async def rpush(self, name: str, *values: str) -> int:
        """Append strings to a list."""

    async def set(self, name: str, value: str, *, ex: int, nx: bool = False) -> bool | None:
        """Set a TTL-bound string, optionally only when absent."""


class FlushScheduler(Protocol):
    """Schedules the worker's future flush without exposing ARQ to ingress."""

    async def schedule_flush_check(self, *, tenant_id: int, delay_s: int) -> None:
        """Schedule using the stable job id ``flush:{tenant_id}``."""


class TelegramWebhookIngress:
    """The post-authentication protocol for one Telegram webhook update.

    A Redis reservation starts before parsing.  It stays only when raw-message
    persistence and all required buffer appends finish, so a transient failure
    cannot turn into a permanent lost Telegram delivery (spec 17.4).
    """

    def __init__(
        self,
        *,
        channel: InboundChannel,
        deduplicator: UpdateDeduplicator,
        identities: IngressIdentityResolver,
        raw_messages: RawMessageStore,
        buffer: TenantBuffer,
    ) -> None:
        if channel.kind != "telegram":
            raise ValueError("TelegramWebhookIngress requires the telegram channel")
        self._channel = channel
        self._deduplicator = deduplicator
        self._identities = identities
        self._raw_messages = raw_messages
        self._buffer = buffer

    async def receive(self, headers: Mapping[str, str], body: bytes) -> None:
        """Verify, deduplicate and durably accept a Telegram update.

        Authentication deliberately comes before JSON parsing.  A forged body
        must neither consume compute in the adapter nor reveal any tenant state.
        """
        self._channel.verify(headers, body)
        payload = _parse_update(body)
        update_id = _update_id(payload)
        if update_id is None:
            return
        if not await self._deduplicator.reserve(update_id):
            return

        try:
            for message in self._channel.parse(payload):
                identity = await self._identities.resolve_or_create(
                    channel=message.channel, external_id=message.external_id
                )
                raw_message_id = await self._raw_messages.persist(
                    identity=identity,
                    message=message,
                )
                if raw_message_id is None or not _is_processable(message):
                    continue
                await self._buffer.append(
                    tenant_id=identity.tenant_id,
                    envelope=_envelope(
                        identity=identity,
                        message=message,
                        raw_message_id=raw_message_id,
                    ),
                )
        except Exception:
            await self._deduplicator.release(update_id)
            raise


class CachedIdentityResolver:
    """Cache identity ids by peppered hash before the database boundary.

    The cache cannot contain an external id.  It contains only the two numeric
    ids needed after lookup, indexed by an HMAC-derived digest.  The delegate
    is responsible for the authorized pre-tenant resolve/create operation.
    """

    def __init__(
        self,
        *,
        cache: IdentityCache,
        delegate: IngressIdentityResolver,
        hash_identity: Callable[[ChannelKind, str], bytes],
        ttl_s: int = 5 * 60,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("identity cache TTL must be positive")
        self._cache = cache
        self._delegate = delegate
        self._hash_identity = hash_identity
        self._ttl_s = ttl_s

    async def resolve_or_create(self, *, channel: ChannelKind, external_id: str) -> IngressIdentity:
        digest = self._hash_identity(channel, external_id)
        key = f"identity:{channel}:{digest.hex()}"
        cached = await self._cache.get(key)
        if cached is not None:
            identity = _cached_identity(cached, digest)
            if identity is not None:
                return identity

        resolved = await self._delegate.resolve_or_create(channel=channel, external_id=external_id)
        # The digest calculated before the database call is authoritative.  A
        # delegate should calculate the same HMAC, but accepting any value it
        # returns would let a bad implementation poison another account's cache.
        identity = IngressIdentity(
            tenant_id=resolved.tenant_id,
            identity_id=resolved.identity_id,
            external_id_hash=digest,
        )
        value = json.dumps(
            {"identity_id": identity.identity_id, "tenant_id": identity.tenant_id},
            separators=(",", ":"),
            sort_keys=True,
        )
        await self._cache.set(key, value, ex=self._ttl_s)
        return identity


class RedisUpdateDeduplicator:
    """Redis ``SET NX EX`` reservation for global Telegram update ids."""

    def __init__(self, redis: RedisIngressClient, *, ttl_s: int = DEDUP_TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("deduplication TTL must be positive")
        self._redis = redis
        self._ttl_s = ttl_s

    async def reserve(self, update_id: int) -> bool:
        if update_id < 0:
            raise ValueError("Telegram update_id must be non-negative")
        reserved = await self._redis.set(_seen_key(update_id), "1", nx=True, ex=self._ttl_s)
        return bool(reserved)

    async def release(self, update_id: int) -> None:
        await self._redis.delete(_seen_key(update_id))


class RedisTenantBuffer:
    """Buffer messages and maintain one future flush per tenant.

    The debounce marker is renewed on every message.  A timer is added only
    for an inactive marker; when it fires early after a renewal, S02-T04's
    ``flush_check`` observes the marker and re-enqueues itself.  This keeps
    the public path bounded while avoiding a timer per message.
    """

    def __init__(
        self,
        *,
        redis: RedisIngressClient,
        scheduler: FlushScheduler,
        debounce_window_s: int,
    ) -> None:
        if debounce_window_s <= 0:
            raise ValueError("debounce window must be positive")
        self._redis = redis
        self._scheduler = scheduler
        self._debounce_window_s = debounce_window_s

    async def append(self, *, tenant_id: int, envelope: dict[str, object]) -> None:
        if tenant_id <= 0:
            raise ValueError("tenant_id must be positive")
        debounce_key = f"debounce:{tenant_id}"
        was_debouncing = bool(await self._redis.exists(debounce_key))
        await self._redis.rpush(
            f"buffer:{tenant_id}",
            json.dumps(envelope, separators=(",", ":"), sort_keys=True),
        )
        await self._redis.set(debounce_key, "1", ex=self._debounce_window_s)
        if not was_debouncing:
            await self._scheduler.schedule_flush_check(
                tenant_id=tenant_id,
                delay_s=self._debounce_window_s,
            )


class DatabaseIdentityResolver:
    """Resolve through the authorized boundary, then read the scoped identity id."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        cipher: ColumnCipher,
        pepper: bytes,
    ) -> None:
        self._sessions = sessions
        self._cipher = cipher
        self._pepper = pepper

    async def resolve_or_create(self, *, channel: ChannelKind, external_id: str) -> IngressIdentity:
        from fittrack.repositories.base import tenant_transaction
        from fittrack.services.identity import IdentityService

        async with self._sessions() as session:
            service = IdentityService(session, self._cipher, self._pepper)
            resolved = await service.resolve_or_create(channel, external_id)
            digest = service.hash_of(channel, external_id)
            await session.commit()

        async with self._sessions() as session, tenant_transaction(session, resolved.tenant_id):
            identity_id = await session.scalar(
                text(
                    "SELECT id FROM channel_identity "
                    "WHERE channel = CAST(:channel AS channel_kind) "
                    "AND external_id_hash = :external_id_hash AND revoked_at IS NULL"
                ),
                {"channel": channel, "external_id_hash": digest},
            )
        if identity_id is None:  # pragma: no cover - committed boundary result must be visible
            raise RuntimeError("resolved identity is unavailable in its tenant context")
        return IngressIdentity(
            tenant_id=resolved.tenant_id,
            identity_id=int(identity_id),
            external_id_hash=digest,
        )


class SqlRawMessageStore:
    """Encrypt and persist the raw update before a buffer can see it."""

    def __init__(self, *, sessions: async_sessionmaker[AsyncSession], cipher: ColumnCipher) -> None:
        self._sessions = sessions
        self._cipher = cipher

    async def persist(self, *, identity: IngressIdentity, message: InboundMessage) -> int | None:
        from fittrack.db.engine import tenant_session
        from fittrack.security.crypto import column_aad

        async with tenant_session(self._sessions, identity.tenant_id) as session:
            row_id = await session.scalar(text("SELECT nextval('raw_message_id_seq')"))
            if row_id is None:  # pragma: no cover - PostgreSQL sequences never return NULL
                raise RuntimeError("raw_message_id_seq returned no id")
            raw_message_id = int(row_id)
            payload = json.dumps(
                dict(message.raw), separators=(",", ":"), ensure_ascii=False, sort_keys=True
            ).encode()
            encrypted = self._cipher.encrypt(
                payload,
                column_aad(
                    tenant_id=identity.tenant_id,
                    table="raw_message",
                    column="payload",
                    row_id=raw_message_id,
                ),
            )
            result = await session.execute(
                text(
                    "INSERT INTO raw_message ("
                    "id, tenant_id, identity_id, channel, channel_message_id, direction, msg_type, "
                    "payload, key_version"
                    ") VALUES ("
                    ":id, :tenant_id, :identity_id, CAST(:channel AS channel_kind), "
                    ":channel_message_id, 'inbound', :msg_type, :payload, :key_version"
                    ") ON CONFLICT (identity_id, channel_message_id) DO NOTHING RETURNING id"
                ),
                {
                    "id": raw_message_id,
                    "tenant_id": identity.tenant_id,
                    "identity_id": identity.identity_id,
                    "channel": message.channel,
                    "channel_message_id": message.channel_message_id,
                    "msg_type": message.kind,
                    "payload": encrypted,
                    "key_version": self._cipher.active_version,
                },
            )
            inserted = result.scalar_one_or_none()
        return int(inserted) if inserted is not None else None


def _parse_update(body: bytes) -> Mapping[str, Any]:
    """Decode only a JSON object; adapters decide whether it is a known update."""
    payload: object = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Telegram webhook payload must be a JSON object")
    return payload


def _cached_identity(value: str | bytes, digest: bytes) -> IngressIdentity | None:
    """Treat malformed cache data as a miss rather than trusting it for RLS."""
    try:
        parsed: object = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    tenant_id = parsed.get("tenant_id")
    identity_id = parsed.get("identity_id")
    if (
        not isinstance(tenant_id, int)
        or isinstance(tenant_id, bool)
        or tenant_id <= 0
        or not isinstance(identity_id, int)
        or isinstance(identity_id, bool)
        or identity_id <= 0
    ):
        return None
    return IngressIdentity(tenant_id=tenant_id, identity_id=identity_id, external_id_hash=digest)


def _update_id(payload: Mapping[str, Any]) -> int | None:
    """Return a valid Telegram update id without accepting bool as an integer."""
    value = payload.get("update_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _seen_key(update_id: int) -> str:
    """Telegram update ids are bot-global, so no external identifier is needed."""
    return f"seen:telegram:{update_id}"


def _is_processable(message: InboundMessage) -> bool:
    """Keep reactions and unsupported media out of the graph buffer."""
    return message.kind in {"text", "voice", "button_reply"}


def _envelope(
    *, identity: IngressIdentity, message: InboundMessage, raw_message_id: int
) -> dict[str, object]:
    """Make the Redis payload safe to inspect: hash, never external_id."""
    return {
        "channel": message.channel,
        "external_id_hash": identity.external_id_hash.hex(),
        "channel_message_id": message.channel_message_id,
        "kind": message.kind,
        "text": message.text,
        "media_ref": message.media_ref,
        "button_payload": message.button_payload,
        "sent_at": message.sent_at.isoformat(),
        "raw_message_id": raw_message_id,
    }
