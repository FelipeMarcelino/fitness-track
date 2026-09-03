"""Ingress sequencing contracts for Sprint 02 task S02-T03."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from fittrack.channels.base import ChannelAuthenticationError, InboundMessage
from fittrack.services.webhook import (
    DedupReservation,
    DedupState,
    IngressIdentity,
    RedisUpdateDeduplicator,
    TelegramWebhookIngress,
    UpdateInFlightError,
)
from fittrack.settings import ChannelKind


@dataclass
class FakeChannel:
    """A channel port that keeps the ingress independent from S02-T02."""

    messages: list[InboundMessage]
    verified: bool = False
    fail_callback_ack: bool = False
    callback_acks: list[str] = field(default_factory=list)
    kind: ClassVar[ChannelKind] = "telegram"

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        self.verified = True
        if headers.get("x-secret") != "valid":
            raise ChannelAuthenticationError

    def parse(self, payload: Mapping[str, Any]) -> list[InboundMessage]:
        assert self.verified
        return self.messages

    async def answer_callback(self, callback_query_id: str) -> None:
        self.callback_acks.append(callback_query_id)
        if self.fail_callback_ack:
            raise RuntimeError("telegram refused answerCallbackQuery")


class FakeDeduplicator:
    def __init__(self, *, state: DedupState = DedupState.ACQUIRED) -> None:
        self.state = state
        self.reserved: list[int] = []
        self.released: list[DedupReservation] = []
        self.completed: list[DedupReservation] = []

    async def reserve(self, update_id: int) -> DedupReservation:
        self.reserved.append(update_id)
        return DedupReservation(
            update_id, self.state, "token" if self.state is DedupState.ACQUIRED else None
        )

    async def complete(self, reservation: DedupReservation) -> bool:
        self.completed.append(reservation)
        return True

    async def release(self, reservation: DedupReservation) -> None:
        self.released.append(reservation)


class FakeIdentityResolver:
    def __init__(self) -> None:
        self.external_ids: list[str] = []
        self.invalidated: list[tuple[str, str]] = []

    async def resolve_or_create(self, *, channel: str, external_id: str) -> IngressIdentity:
        self.external_ids.append(external_id)
        return IngressIdentity(tenant_id=31, identity_id=41, external_id_hash=b"hashed-chat")

    async def invalidate(self, *, channel: str, external_id: str) -> None:
        self.invalidated.append((channel, external_id))


class FakeRawMessages:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[InboundMessage] = []

    async def persist(self, *, identity: IngressIdentity, message: InboundMessage) -> int:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("database unavailable")
        return 71


class FakeBuffer:
    def __init__(self) -> None:
        self.envelopes: list[dict[str, object]] = []

    async def append(self, *, tenant_id: int, envelope: dict[str, object]) -> None:
        assert tenant_id == 31
        self.envelopes.append(envelope)


def inbound(*, kind: str = "text") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        external_id="private-chat-id",
        channel_message_id="message-11",
        kind=kind,  # type: ignore[arg-type]
        text="treino feito" if kind == "text" else None,
        media_ref=None,
        button_payload=None,
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={"message": {"chat": {"id": "private-chat-id"}}},
    )


def ingress(
    *,
    messages: list[InboundMessage] | None = None,
    deduplicator: FakeDeduplicator | None = None,
    raw_messages: FakeRawMessages | None = None,
) -> tuple[
    TelegramWebhookIngress,
    FakeDeduplicator,
    FakeIdentityResolver,
    FakeRawMessages,
    FakeBuffer,
]:
    seen = deduplicator or FakeDeduplicator()
    identities = FakeIdentityResolver()
    persisted = raw_messages or FakeRawMessages()
    buffer = FakeBuffer()
    return (
        TelegramWebhookIngress(
            channel=FakeChannel(messages or [inbound()]),
            deduplicator=seen,
            identities=identities,
            raw_messages=persisted,
            buffer=buffer,
        ),
        seen,
        identities,
        persisted,
        buffer,
    )


async def test_a_completed_duplicate_returns_before_identity_lookup() -> None:
    service, seen, identities, persisted, buffer = ingress(
        deduplicator=FakeDeduplicator(state=DedupState.COMPLETED)
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 901}')

    assert seen.reserved == [901]
    assert identities.external_ids == []
    assert persisted.messages == []
    assert buffer.envelopes == []


async def test_a_failure_after_the_reservation_releases_it_for_redelivery() -> None:
    service, seen, _, _, buffer = ingress(raw_messages=FakeRawMessages(fail=True))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.receive({"x-secret": "valid"}, b'{"update_id": 902}')

    assert [reservation.update_id for reservation in seen.released] == [902]
    assert buffer.envelopes == []


@pytest.mark.parametrize("kind", ["other", "image", "document"])
async def test_non_processable_updates_are_persisted_but_not_buffered(kind: str) -> None:
    service, _, _, persisted, buffer = ingress(messages=[inbound(kind=kind)])

    await service.receive({"x-secret": "valid"}, b'{"update_id": 903}')

    assert len(persisted.messages) == 1
    assert buffer.envelopes == []


async def test_a_buffered_envelope_contains_the_hash_and_never_the_external_id() -> None:
    service, _, _, _, buffer = ingress()

    await service.receive({"x-secret": "valid"}, b'{"update_id": 904}')

    assert buffer.envelopes == [
        {
            "channel": "telegram",
            "external_id_hash": "6861736865642d63686174",
            "channel_message_id": "message-11",
            "kind": "text",
            "text": "treino feito",
            "media_ref": None,
            "duration_s": None,
            "button_payload": None,
            "sent_at": "2026-01-01T00:00:00+00:00",
            "raw_message_id": 71,
        }
    ]
    assert "private-chat-id" not in repr(buffer.envelopes)


async def test_an_inflight_duplicate_is_not_acknowledged() -> None:
    service, seen, identities, persisted, buffer = ingress(
        deduplicator=FakeDeduplicator(state=DedupState.IN_FLIGHT)
    )

    with pytest.raises(UpdateInFlightError):
        await service.receive({"x-secret": "valid"}, b'{"update_id": 905}')

    assert seen.reserved == [905]
    assert identities.external_ids == []
    assert persisted.messages == []
    assert buffer.envelopes == []


async def test_accept_runs_the_same_sequence_as_receive_without_verifying() -> None:
    """The poller (S02-T08) hands over an already-decoded update, not bytes.

    Polling authenticates by holding the bot token, not a per-request secret
    (spec 18.2), so `accept` skips `verify` and `_parse_update` and starts
    straight from the payload — everything after that is identical to `receive`.
    `FakeChannel.parse` asserts `verified`, so this constructs the channel
    already marked as such: polling's authentication happened one layer down,
    at the client that holds the token, not per update.
    """
    seen = FakeDeduplicator()
    identities = FakeIdentityResolver()
    persisted = FakeRawMessages()
    buffer = FakeBuffer()
    service = TelegramWebhookIngress(
        channel=FakeChannel([inbound()], verified=True),
        deduplicator=seen,
        identities=identities,
        raw_messages=persisted,
        buffer=buffer,
    )

    await service.accept({"update_id": 906})

    assert seen.reserved == [906]
    assert len(identities.external_ids) == 1
    assert len(persisted.messages) == 1
    assert len(buffer.envelopes) == 1


# --------------------------------------------------------------------------- #
# Membership revocation (spec 18.2 review) — `my_chat_member` blocked/kicked
# --------------------------------------------------------------------------- #


class FakeRevoker:
    def __init__(self) -> None:
        self.revoked: list[tuple[int, int]] = []

    async def revoke_identity(
        self, *, identity_id: int, tenant_id: int, revoked_at: datetime
    ) -> None:
        self.revoked.append((identity_id, tenant_id))


def membership_event(*, status: str, update_id: int) -> InboundMessage:
    """What the adapter hands back for `my_chat_member` — `kind="other"`, raw intact."""
    return InboundMessage(
        channel="telegram",
        external_id="private-chat-id",
        channel_message_id=f"telegram-update:{update_id}",
        kind="other",
        text=None,
        media_ref=None,
        button_payload=None,
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={
            "update_id": update_id,
            "my_chat_member": {
                "chat": {"id": "private-chat-id", "type": "private"},
                "new_chat_member": {"status": status},
            },
        },
    )


async def test_a_block_event_revokes_the_resolved_identity() -> None:
    seen = FakeDeduplicator()
    identities = FakeIdentityResolver()
    persisted = FakeRawMessages()
    buffer = FakeBuffer()
    revoker = FakeRevoker()
    service = TelegramWebhookIngress(
        channel=FakeChannel([membership_event(status="kicked", update_id=990)]),
        deduplicator=seen,
        identities=identities,
        raw_messages=persisted,
        buffer=buffer,
        revoker=revoker,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 990}')

    assert revoker.revoked == [(41, 31)]
    assert buffer.envelopes == [], "a membership event is never buffered either way"


async def test_a_user_leaving_also_revokes() -> None:
    """`left` and `kicked` both mean the bot cannot reach this chat anymore."""
    revoker = FakeRevoker()
    service = TelegramWebhookIngress(
        channel=FakeChannel([membership_event(status="left", update_id=994)]),
        deduplicator=FakeDeduplicator(),
        identities=FakeIdentityResolver(),
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
        revoker=revoker,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 994}')

    assert revoker.revoked == [(41, 31)]


async def test_an_ordinary_message_does_not_revoke() -> None:
    revoker = FakeRevoker()
    service = TelegramWebhookIngress(
        channel=FakeChannel([inbound()]),
        deduplicator=FakeDeduplicator(),
        identities=FakeIdentityResolver(),
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
        revoker=revoker,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 991}')

    assert revoker.revoked == []


async def test_a_reachable_membership_change_does_not_revoke() -> None:
    """`member`/`administrator`/`restricted` are still reachable — only a
    block or a departure is (`REVOKED_MEMBER_STATUSES`).
    """
    revoker = FakeRevoker()
    service = TelegramWebhookIngress(
        channel=FakeChannel([membership_event(status="administrator", update_id=992)]),
        deduplicator=FakeDeduplicator(),
        identities=FakeIdentityResolver(),
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
        revoker=revoker,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 992}')

    assert revoker.revoked == []


async def test_without_a_revoker_a_block_event_is_still_persisted_but_not_acted_on() -> None:
    """The default keeps every caller that predates this feature working."""
    service, _, _, persisted, buffer = ingress(
        messages=[membership_event(status="kicked", update_id=993)]
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 993}')

    assert len(persisted.messages) == 1
    assert buffer.envelopes == []


async def test_a_block_event_invalidates_the_cached_identity() -> None:
    """Without this, a resolver caching for 5 minutes (spec: identity cache)
    keeps handing out the now-revoked identity until the TTL expires — a
    user who unblocks and messages again during that window would resolve
    against a destination `revoked_at` already marks unreachable (S02-T08
    review).
    """
    identities = FakeIdentityResolver()
    revoker = FakeRevoker()
    service = TelegramWebhookIngress(
        channel=FakeChannel([membership_event(status="kicked", update_id=995)]),
        deduplicator=FakeDeduplicator(),
        identities=identities,
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
        revoker=revoker,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 995}')

    assert identities.invalidated == [("telegram", "private-chat-id")]


async def test_a_resolver_without_invalidate_does_not_break_revocation() -> None:
    """Duck-typed on purpose: `IngressIdentityResolver` never promised it."""

    class BareResolver:
        async def resolve_or_create(self, *, channel: str, external_id: str) -> IngressIdentity:
            return IngressIdentity(tenant_id=31, identity_id=41, external_id_hash=b"h")

    revoker = FakeRevoker()
    service = TelegramWebhookIngress(
        channel=FakeChannel([membership_event(status="kicked", update_id=996)]),
        deduplicator=FakeDeduplicator(),
        identities=BareResolver(),
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
        revoker=revoker,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 996}')

    assert revoker.revoked == [(41, 31)]


async def test_a_failed_cache_invalidation_does_not_trigger_a_redelivery() -> None:
    """A retry after a successful revoke would re-`resolve_or_create` against
    an identity `resolve_or_create` can no longer see (spec 5.2's active-only
    unique index) and mint a fresh tenant for the account just revoked — the
    worse of the two outcomes invalidation failing can lead to, so this stays
    best-effort rather than propagating (S02-T08 review).
    """

    class ExplodingIdentities:
        async def resolve_or_create(self, *, channel: str, external_id: str) -> IngressIdentity:
            return IngressIdentity(tenant_id=31, identity_id=41, external_id_hash=b"h")

        async def invalidate(self, *, channel: str, external_id: str) -> None:
            raise RuntimeError("redis unavailable")

    seen = FakeDeduplicator()
    revoker = FakeRevoker()
    service = TelegramWebhookIngress(
        channel=FakeChannel([membership_event(status="kicked", update_id=1001)]),
        deduplicator=seen,
        identities=ExplodingIdentities(),
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
        revoker=revoker,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 1001}')

    assert revoker.revoked == [(41, 31)]
    assert seen.released == [], "the reservation must not be released over this"
    assert len(seen.completed) == 1, "the update must still be marked complete"


# --------------------------------------------------------------------------- #
# Callback acknowledgement (spec 18.2 review) — before anything is queued
# --------------------------------------------------------------------------- #


def button_press(*, channel_message_id: str = "press:123") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        external_id="private-chat-id",
        channel_message_id=channel_message_id,
        kind="button_reply",
        text=None,
        media_ref=None,
        button_payload="set_confirmed",
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={"callback_query": {"id": "123", "from": {"id": "private-chat-id"}}},
    )


async def test_a_button_reply_is_acknowledged_before_anything_else() -> None:
    channel = FakeChannel([button_press()])
    service = TelegramWebhookIngress(
        channel=channel,
        deduplicator=FakeDeduplicator(),
        identities=FakeIdentityResolver(),
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 997}')

    assert channel.callback_acks == ["press:123"]


async def test_a_text_message_is_never_acknowledged_as_a_callback() -> None:
    channel = FakeChannel([inbound()])
    service = TelegramWebhookIngress(
        channel=channel,
        deduplicator=FakeDeduplicator(),
        identities=FakeIdentityResolver(),
        raw_messages=FakeRawMessages(),
        buffer=FakeBuffer(),
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 998}')

    assert channel.callback_acks == []


async def test_a_failed_acknowledgement_does_not_block_the_durable_path() -> None:
    """The button press is still persisted and buffered — Telegram's spinner
    times out on its own; a lost button press does not.
    """
    channel = FakeChannel([button_press()], fail_callback_ack=True)
    persisted = FakeRawMessages()
    buffer = FakeBuffer()
    service = TelegramWebhookIngress(
        channel=channel,
        deduplicator=FakeDeduplicator(),
        identities=FakeIdentityResolver(),
        raw_messages=persisted,
        buffer=buffer,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 999}')

    assert channel.callback_acks == ["press:123"]
    assert len(persisted.messages) == 1
    assert len(buffer.envelopes) == 1


async def test_a_channel_without_answer_callback_does_not_break_button_replies() -> None:
    """Duck-typed on purpose: `InboundChannel` never promised it either."""

    @dataclass
    class BareChannel:
        messages: list[InboundMessage]
        kind: ClassVar[ChannelKind] = "telegram"

        def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
            return None

        def parse(self, payload: Mapping[str, Any]) -> list[InboundMessage]:
            return self.messages

    buffer = FakeBuffer()
    service = TelegramWebhookIngress(
        channel=BareChannel([button_press()]),
        deduplicator=FakeDeduplicator(),
        identities=FakeIdentityResolver(),
        raw_messages=FakeRawMessages(),
        buffer=buffer,
    )

    await service.receive({"x-secret": "valid"}, b'{"update_id": 1000}')

    assert len(buffer.envelopes) == 1


# --------------------------------------------------------------------------- #
# Deduplication namespaced by bot (spec 18.2 review)
# --------------------------------------------------------------------------- #


class KeyRecordingRedis:
    """The Redis surface `RedisUpdateDeduplicator.reserve` needs, kept real."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def set(self, name: str, value: str, *, ex: int, nx: bool = False) -> bool | None:
        if nx and name in self._data:
            return False
        self._data[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self._data.get(name)


async def test_the_deduplicator_namespaces_its_key_by_bot() -> None:
    """A dev Redis volume that outlives a `TELEGRAM_BOT_TOKEN` change must not
    hand the new bot the old bot's completed reservation — `reserve` would
    report an update the new bot never saw as an already-processed duplicate
    and silently drop it.
    """
    redis = KeyRecordingRedis()
    old_bot = RedisUpdateDeduplicator(redis, bot_fingerprint="bot-a")  # type: ignore[arg-type]
    new_bot = RedisUpdateDeduplicator(redis, bot_fingerprint="bot-b")  # type: ignore[arg-type]

    await old_bot.reserve(500)
    reservation = await new_bot.reserve(500)

    assert reservation.state is DedupState.ACQUIRED
