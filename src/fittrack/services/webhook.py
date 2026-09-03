"""Verified webhook ingestion without coupling it to a concrete channel.

The HTTP route owns status codes; this service owns the durable sequence after
authentication.  Its collaborators are small ports so the Telegram adapter
can be developed independently (Sprint 02 tasks S02-T02 and S02-T03).
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fittrack.channels.base import InboundMessage
from fittrack.security.crypto import ColumnCipher
from fittrack.settings import ChannelKind

logger = logging.getLogger(__name__)

DEDUP_TTL_S = 24 * 60 * 60
PROCESSING_TTL_S = 60
BUFFER_TTL_S = 60 * 60


@dataclass(frozen=True, slots=True)
class IngressIdentity:
    """The tenant-scoped identity needed after the pre-tenant boundary."""

    tenant_id: int
    identity_id: int
    external_id_hash: bytes


class DedupState(StrEnum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    IN_FLIGHT = "in_flight"


@dataclass(frozen=True, slots=True)
class DedupReservation:
    update_id: int
    state: DedupState
    token: str | None = None


class UpdateInFlightError(RuntimeError):
    """A concurrent delivery must retry instead of being acknowledged."""


class UpdateDeduplicator(Protocol):
    """Reserves a Telegram update until all durable work has succeeded."""

    async def reserve(self, update_id: int) -> DedupReservation:
        """Classify this delivery as acquired, complete, or still processing."""

    async def complete(self, reservation: DedupReservation) -> bool:
        """Atomically mark an acquired reservation completed for 24 hours."""

    async def release(self, reservation: DedupReservation) -> None:
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

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> object:
        """Execute the generation-guarded identity-cache scripts."""


class RawMessageStore(Protocol):
    """Persists encrypted inbound payloads under the resolved tenant context."""

    async def persist(self, *, identity: IngressIdentity, message: InboundMessage) -> int:
        """Return the inserted or SQL-deduplicated raw-message id."""


class TenantBuffer(Protocol):
    """Appends a processing envelope and renews its tenant debounce timer."""

    async def append(self, *, tenant_id: int, envelope: dict[str, object]) -> None:
        """Write an envelope that contains no plaintext external identifier."""


class RedisIngressClient(Protocol):
    """The Redis commands owned by webhook intake (not worker drain)."""

    async def delete(self, *names: str) -> int:
        """Delete one or more keys."""

    async def expire(self, name: str, time: int) -> bool:
        """Apply a bounded lifetime to sensitive list data."""

    async def get(self, name: str) -> str | bytes | None:
        """Read a string key."""

    async def rpush(self, name: str, *values: str) -> int:
        """Append strings to a list."""

    async def set(self, name: str, value: str, *, ex: int, nx: bool = False) -> bool | None:
        """Set a TTL-bound string, optionally only when absent."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> int:
        """Run the compare-and-set scripts used by reservations."""


class FlushScheduler(Protocol):
    """Schedules the worker's future flush without exposing ARQ to ingress."""

    async def schedule_flush_check(self, *, tenant_id: int, delay_s: int) -> None:
        """Schedule using the stable job id ``flush:{tenant_id}``."""


class IdentityRevoker(Protocol):
    """Marks an identity unreachable through the security boundary of §19.1."""

    async def revoke_identity(
        self, *, identity_id: int, tenant_id: int, revoked_at: datetime
    ) -> None:
        """Set ``revoked_at``, idempotently, for one identity of one tenant."""


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
        revoker: IdentityRevoker | None = None,
    ) -> None:
        if channel.kind != "telegram":
            raise ValueError("TelegramWebhookIngress requires the telegram channel")
        self._channel = channel
        self._deduplicator = deduplicator
        self._identities = identities
        self._raw_messages = raw_messages
        self._buffer = buffer
        # Optional so every existing fixture that builds one of these keeps
        # working: a deployment that omits it just never revokes on a block,
        # which is the status quo `ingress_wiring.py`'s production wiring fixes.
        self._revoker = revoker

    async def receive(self, headers: Mapping[str, str], body: bytes) -> None:
        """Verify, deduplicate and durably accept a Telegram update.

        Authentication deliberately comes before JSON parsing.  A forged body
        must neither consume compute in the adapter nor reveal any tenant state.
        """
        self.verify(headers)
        await self.accept(_parse_update(body))

    async def accept(self, payload: Mapping[str, Any]) -> None:
        """Deduplicate and durably accept an already-decoded update.

        `receive` is `verify` plus this. Polling (S02-T08) has no per-request
        secret to verify — `getUpdates` authenticates by holding the bot token,
        once, at the client — so it calls this directly with the JSON object
        Telegram already handed it, skipping both `verify` and `_parse_update`.
        """
        update_id = _update_id(payload)
        if update_id is None:
            return
        reservation = await self._deduplicator.reserve(update_id)
        if reservation.state is DedupState.COMPLETED:
            return
        if reservation.state is DedupState.IN_FLIGHT:
            raise UpdateInFlightError

        try:
            for message in self._channel.parse(payload):
                if message.kind == "button_reply":
                    # Before anything else, and best-effort: Telegram leaves
                    # the client's progress indicator spinning until this is
                    # answered or it times out on its own, and a raised error
                    # here must not cost the button press its durable record
                    # (spec 18.2 review).
                    answer_callback = getattr(self._channel, "answer_callback", None)
                    if callable(answer_callback):
                        try:
                            await answer_callback(message.channel_message_id)
                        except Exception:
                            logger.exception("answerCallbackQuery failed")
                identity = await self._identities.resolve_or_create(
                    channel=message.channel, external_id=message.external_id
                )
                raw_message_id = await self._raw_messages.persist(
                    identity=identity,
                    message=message,
                )
                if self._revoker is not None and _is_membership_revocation(message):
                    # Ahead of the `_is_processable` gate below: this kind is
                    # never buffered either way, and revocation is the whole
                    # reason `my_chat_member` is in `ALLOWED_UPDATES` (spec
                    # 18.2) — without it, `revoked_at` never moves until the
                    # next outbound send fails.
                    await self._revoker.revoke_identity(
                        identity_id=identity.identity_id,
                        tenant_id=identity.tenant_id,
                        revoked_at=datetime.now(UTC),
                    )
                    # The cache can still be holding the pre-revocation
                    # identity for up to its TTL; without invalidating it, a
                    # user who unblocks and messages again during that window
                    # would resolve straight back to it (spec 18.2 review).
                    # `CachedIdentityResolver.invalidate` bumps a generation
                    # counter an in-flight fill also checks, so this closes
                    # the race with a concurrent resolve too, not just the
                    # stale-read case.
                    # Best-effort: a failure here must not re-raise and
                    # trigger a redelivery. `revoke_identity` already
                    # committed, and `resolve_or_create` only matches active
                    # identities (spec 5.2's partial unique index) — a retry
                    # of *this* update would find none and mint a fresh
                    # tenant for the account we just finished revoking. A
                    # stale cache entry for up to its TTL is the smaller of
                    # the two costs. Tracked for a proper fix in #29's
                    # follow-up territory: resolving a membership-revocation
                    # event needs a lookup that includes revoked rows, not
                    # just a resilient cache invalidation.
                    invalidate = getattr(self._identities, "invalidate", None)
                    if callable(invalidate):
                        try:
                            await invalidate(
                                channel=message.channel, external_id=message.external_id
                            )
                        except Exception:
                            logger.exception("identity cache invalidation failed after revocation")
                if not _is_processable(message):
                    continue
                await self._buffer.append(
                    tenant_id=identity.tenant_id,
                    envelope=_envelope(
                        identity=identity,
                        message=message,
                        raw_message_id=raw_message_id,
                    ),
                )
            if not await self._deduplicator.complete(reservation):
                raise UpdateInFlightError("lost the Telegram update reservation")
        except Exception:
            await self._deduplicator.release(reservation)
            raise

    def verify(self, headers: Mapping[str, str]) -> None:
        """Use Telegram's header-only secret before FastAPI reads the body."""
        self._channel.verify(headers, b"")


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
        key, generation_key = _identity_cache_keys(channel, digest)
        snapshot = await self._cache.eval(_IDENTITY_CACHE_SNAPSHOT, 2, key, generation_key)
        generation, cached = _identity_snapshot(snapshot)
        if generation is not None and cached is not None:
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
            {
                "generation": generation,
                "identity_id": identity.identity_id,
                "tenant_id": identity.tenant_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        filled = await self._cache.eval(
            _IDENTITY_CACHE_FILL,
            2,
            key,
            generation_key,
            generation or "0",
            value,
            self._ttl_s,
        )
        if filled:
            return identity
        # Revocation won the race while the authorized lookup was in flight;
        # start again, so no stale tenant/id pair becomes observable.
        return await self.resolve_or_create(channel=channel, external_id=external_id)

    async def invalidate(self, *, channel: ChannelKind, external_id: str) -> None:
        """Drop the exact mapping the authorized revocation path changed."""
        digest = self._hash_identity(channel, external_id)
        key, generation_key = _identity_cache_keys(channel, digest)
        await self._cache.eval(_IDENTITY_CACHE_INVALIDATE, 2, key, generation_key)


class RedisUpdateDeduplicator:
    """Redis ``SET NX EX`` reservation for global Telegram update ids.

    Namespaced by ``bot_fingerprint``: update ids are global to *one* bot's
    stream (module docstring of the key builder below), and a dev Redis
    volume that outlives a `TELEGRAM_BOT_TOKEN` change must not let the new
    bot inherit the old one's completed reservations — that would make
    `reserve` report an update the new bot never saw as an already-processed
    duplicate, silently dropping it (spec 18.2 review).
    """

    def __init__(
        self,
        redis: RedisIngressClient,
        *,
        bot_fingerprint: str,
        ttl_s: int = DEDUP_TTL_S,
        processing_ttl_s: int = PROCESSING_TTL_S,
    ) -> None:
        if ttl_s <= 0 or processing_ttl_s <= 0:
            raise ValueError("deduplication TTL must be positive")
        self._redis = redis
        self._bot_fingerprint = bot_fingerprint
        self._ttl_s = ttl_s
        self._processing_ttl_s = processing_ttl_s

    def _key(self, update_id: int) -> str:
        return _seen_key(update_id, self._bot_fingerprint)

    async def reserve(self, update_id: int) -> DedupReservation:
        if update_id < 0:
            raise ValueError("Telegram update_id must be non-negative")
        token = f"processing:{secrets.token_urlsafe(16)}"
        key = self._key(update_id)
        reserved = await self._redis.set(key, token, nx=True, ex=self._processing_ttl_s)
        if reserved:
            return DedupReservation(update_id, DedupState.ACQUIRED, token)
        existing = await self._redis.get(key)
        if existing in {"completed", b"completed"}:
            return DedupReservation(update_id, DedupState.COMPLETED)
        return DedupReservation(update_id, DedupState.IN_FLIGHT)

    async def complete(self, reservation: DedupReservation) -> bool:
        if reservation.state is not DedupState.ACQUIRED or reservation.token is None:
            return False
        changed = await self._redis.eval(
            _COMPLETE_RESERVATION,
            1,
            self._key(reservation.update_id),
            reservation.token,
            self._ttl_s,
        )
        return bool(changed)

    async def release(self, reservation: DedupReservation) -> None:
        if reservation.state is not DedupState.ACQUIRED or reservation.token is None:
            return
        await self._redis.eval(
            _RELEASE_RESERVATION, 1, self._key(reservation.update_id), reservation.token
        )


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
        raw_message_id = envelope.get("raw_message_id")
        if not isinstance(raw_message_id, int) or isinstance(raw_message_id, bool):
            raise ValueError("buffer envelope requires an integer raw_message_id")
        await self._redis.eval(
            _APPEND_ENVELOPE,
            3,
            f"buffer:{tenant_id}",
            f"debounce:{tenant_id}",
            f"buffered:{tenant_id}:{raw_message_id}",
            json.dumps(envelope, separators=(",", ":"), sort_keys=True),
            BUFFER_TTL_S,
            self._debounce_window_s,
        )
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

    async def persist(self, *, identity: IngressIdentity, message: InboundMessage) -> int:
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
            if inserted is not None:
                return int(inserted)
            existing = await session.scalar(
                text(
                    "SELECT id FROM raw_message WHERE identity_id = :identity_id "
                    "AND channel_message_id = :channel_message_id"
                ),
                {
                    "identity_id": identity.identity_id,
                    "channel_message_id": message.channel_message_id,
                },
            )
        if existing is None:  # pragma: no cover - conflict row must be visible in the transaction
            raise RuntimeError("raw-message deduplication conflict has no existing row")
        return int(existing)


class SqlIdentityRevoker:
    """`revoke_channel_identity` (migration 0004), reached from the ingress.

    The same SQL function `PostgresOutboundQueueStore.revoke_identity`
    (services/outbound.py) calls when a send discovers the block reactively.
    A separate, narrow class rather than reusing that one: it bundles retry
    and dead-letter concerns the ingress has no reason to depend on, and this
    is the one method of it ingress needs.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def revoke_identity(
        self, *, identity_id: int, tenant_id: int, revoked_at: datetime
    ) -> None:
        from fittrack.db.engine import tenant_session

        async with tenant_session(self._sessions, tenant_id) as session:
            revoked = await session.scalar(
                text("SELECT revoke_channel_identity(:identity_id, :tenant_id, :revoked_at)"),
                {"identity_id": identity_id, "tenant_id": tenant_id, "revoked_at": revoked_at},
            )
            if revoked is not True:
                raise LookupError("identity does not belong to tenant")


_COMPLETE_RESERVATION = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], 'completed', 'EX', ARGV[2])
  return 1
end
return 0
"""
_RELEASE_RESERVATION = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_APPEND_ENVELOPE = """
if redis.call('SET', KEYS[3], '1', 'NX', 'EX', ARGV[2]) then
  redis.call('RPUSH', KEYS[1], ARGV[1])
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
redis.call('SET', KEYS[2], '1', 'EX', ARGV[3])
return 1
"""
_IDENTITY_CACHE_SNAPSHOT = """
return {redis.call('GET', KEYS[2]) or '0', redis.call('GET', KEYS[1]) or ''}
"""
_IDENTITY_CACHE_FILL = """
if (redis.call('GET', KEYS[2]) or '0') == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""
_IDENTITY_CACHE_INVALIDATE = """
redis.call('INCR', KEYS[2])
redis.call('DEL', KEYS[1])
return 1
"""


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


def _identity_cache_keys(channel: ChannelKind, digest: bytes) -> tuple[str, str]:
    stem = f"identity:{channel}:{digest.hex()}"
    return stem, f"{stem}:generation"


def _identity_snapshot(value: object) -> tuple[str | None, str | bytes | None]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None, None
    generation, cached = value
    if isinstance(generation, bytes):
        generation = generation.decode()
    if not isinstance(generation, str) or not isinstance(cached, (str, bytes)):
        return None, None
    return generation, cached or None


def _update_id(payload: Mapping[str, Any]) -> int | None:
    """Return a valid Telegram update id without accepting bool as an integer."""
    value = payload.get("update_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _seen_key(update_id: int, bot_fingerprint: str) -> str:
    """Telegram update ids are global to one bot's stream, not across bots."""
    return f"seen:telegram:{bot_fingerprint}:{update_id}"


def _is_processable(message: InboundMessage) -> bool:
    """Keep reactions and unsupported media out of the graph buffer."""
    return message.kind in {"text", "voice", "button_reply"}


# The statuses that mean the bot can no longer reach this chat. `restricted`
# and `administrator` are membership changes too, but reachable ones — only
# these two say every future send from `deliver` would refuse (spec 18.4's
# ACCOUNT class), which is the same condition an outbound failure discovers
# reactively, just found out about it first (spec 18.2).
REVOKED_MEMBER_STATUSES = frozenset({"kicked", "left"})


def _is_membership_revocation(message: InboundMessage) -> bool:
    """Whether this is Telegram's `my_chat_member` telling us we were blocked.

    Read from `message.raw`, deliberately: the adapter leaves `kind="other"`
    here on purpose (S02-T02's `test_a_block_is_recorded_so_the_identity_can_
    be_revoked`; "the adapter writes nothing, it makes the event visible with
    its payload intact so the layer that owns identity can act on it") —
    translating the raw event into a neutral `kind` would be a channel
    deciding, at parse time, something only this Telegram-specific ingress
    needs to know. `TelegramWebhookIngress` already knows it is Telegram; a
    channel-neutral caller of `_is_processable` never would.
    """
    event = message.raw.get("my_chat_member")
    if not isinstance(event, dict):
        return False
    new_status = (event.get("new_chat_member") or {}).get("status")
    return new_status in REVOKED_MEMBER_STATUSES


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
        # The transcription service refuses a recording past the ceiling of
        # 11.3 with a fixed reply, and it needs the number to do so.
        "duration_s": message.media_duration_s,
        "button_payload": message.button_payload,
        "sent_at": message.sent_at.isoformat(),
        "raw_message_id": raw_message_id,
    }
