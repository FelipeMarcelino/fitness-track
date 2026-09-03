"""Ingress sequencing contracts for Sprint 02 task S02-T03."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from fittrack.channels.base import ChannelAuthenticationError, InboundMessage
from fittrack.services.webhook import (
    DedupReservation,
    DedupState,
    IngressIdentity,
    TelegramWebhookIngress,
    UpdateInFlightError,
)
from fittrack.settings import ChannelKind


@dataclass
class FakeChannel:
    """A channel port that keeps the ingress independent from S02-T02."""

    messages: list[InboundMessage]
    verified: bool = False
    kind: ClassVar[ChannelKind] = "telegram"

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        self.verified = True
        if headers.get("x-secret") != "valid":
            raise ChannelAuthenticationError

    def parse(self, payload: Mapping[str, Any]) -> list[InboundMessage]:
        assert self.verified
        return self.messages


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

    async def resolve_or_create(self, *, channel: str, external_id: str) -> IngressIdentity:
        self.external_ids.append(external_id)
        return IngressIdentity(tenant_id=31, identity_id=41, external_id_hash=b"hashed-chat")


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
