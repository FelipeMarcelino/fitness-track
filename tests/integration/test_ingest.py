"""Persistence of an inbound delivery (§4.1, §5.2)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fittrack.channels.whatsapp.ingest import Ingest
from fittrack.channels.whatsapp.payload import InboundMessage
from fittrack.crypto.aesgcm import Encryptor, KeyRing

pytestmark = pytest.mark.integration

ENCRYPTOR = Encryptor(KeyRing({1: os.urandom(32)}, current_version=1))


class FakeBuffer:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, dict[str, Any]]] = []

    async def push(self, bsuid: str, message: dict[str, Any]) -> None:
        self.pushed.append((bsuid, message))


def _message(message_id: str = "wamid.1", **kw: Any) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        bsuid=kw.pop("bsuid", "BSUID-ingest"),
        msg_type=kw.pop("msg_type", "text"),
        timestamp=1,
        text=kw.pop("text", "supino 80kg 8 reps"),
        **kw,
    )


@pytest.fixture
def ingest(app_dsn: str) -> tuple[Ingest, FakeBuffer]:
    """Connects as fittrack_app, the way production does.

    An earlier version used the owner engine, which is a superuser and bypasses
    RLS -- so the suite passed while every write would have been rejected in
    production. The whole point of this fixture is that the policies are live.
    """
    engine = create_async_engine(app_dsn)
    buffer = FakeBuffer()
    return Ingest(engine, ENCRYPTOR, buffer), buffer


async def test_first_contact_creates_the_tenant_before_the_message(
    ingest: tuple[Ingest, FakeBuffer], owner_conn: AsyncConnection
) -> None:
    """raw_message.tenant_id is NOT NULL ON DELETE CASCADE so an erasure cannot
    leave orphaned message bodies (§5.2). That makes the upsert order a
    correctness requirement, not a preference."""
    service, _ = ingest

    await service.accept_message(_message(), {"entry": []})

    row = await owner_conn.execute(
        text(
            "SELECT t.bsuid, r.msg_type FROM raw_message r "
            "JOIN tenant t ON t.id = r.tenant_id WHERE r.wa_message_id = 'wamid.1'"
        )
    )
    bsuid, msg_type = row.one()
    assert bsuid == "BSUID-ingest"
    assert msg_type == "text"


async def test_payload_is_stored_encrypted(
    ingest: tuple[Ingest, FakeBuffer], owner_conn: AsyncConnection
) -> None:
    service, _ = ingest
    envelope = {"entry": [{"secret": "conteudo do usuario"}]}

    await service.accept_message(_message("wamid.enc"), envelope)

    row = await owner_conn.execute(
        text("SELECT payload FROM raw_message WHERE wa_message_id = 'wamid.enc'")
    )
    stored = bytes(row.scalar_one())
    assert b"conteudo do usuario" not in stored
    assert ENCRYPTOR.decrypt_json(stored) == envelope


async def test_redelivery_does_not_duplicate(
    ingest: tuple[Ingest, FakeBuffer], owner_conn: AsyncConnection
) -> None:
    """Meta redelivers whatever it did not get a 200 for. A redelivery reaching
    the buffer twice would double the workout volume."""
    service, buffer = ingest
    message = _message("wamid.dup")

    await service.accept_message(message, {"entry": []})
    await service.accept_message(message, {"entry": []})

    row = await owner_conn.execute(
        text("SELECT count(*) FROM raw_message WHERE wa_message_id = 'wamid.dup'")
    )
    assert row.scalar_one() == 1
    assert len(buffer.pushed) == 1


async def test_non_actionable_message_is_stored_but_not_queued(
    ingest: tuple[Ingest, FakeBuffer], owner_conn: AsyncConnection
) -> None:
    """A reaction is kept for audit and costs nothing downstream (§18.3)."""
    service, buffer = ingest

    await service.accept_message(
        _message("wamid.react", msg_type="reaction", text=None), {"entry": []}
    )

    row = await owner_conn.execute(
        text("SELECT count(*) FROM raw_message WHERE wa_message_id = 'wamid.react'")
    )
    assert row.scalar_one() == 1
    assert buffer.pushed == []


async def test_conversation_window_is_refreshed(
    ingest: tuple[Ingest, FakeBuffer], owner_conn: AsyncConnection
) -> None:
    """The 24h window decides whether the coach may send free text or must use
    a template (§14). It is anchored on the last inbound message."""
    service, _ = ingest

    await service.accept_message(_message("wamid.win", bsuid="BSUID-window"), {})

    row = await owner_conn.execute(
        text(
            "SELECT last_inbound_at > now() - interval '1 minute' "
            "FROM conversation_window w JOIN tenant t ON t.id = w.tenant_id "
            "WHERE t.bsuid = 'BSUID-window'"
        )
    )
    assert row.scalar_one() is True


async def test_writes_succeed_under_row_level_security(
    ingest: tuple[Ingest, FakeBuffer], owner_conn: AsyncConnection
) -> None:
    """The regression this file exists for.

    raw_message and conversation_window are RLS-forced, and fittrack_app is not
    a superuser, so a transaction that does not SET LOCAL app.tenant_id has its
    writes rejected and the delivery is lost.
    """
    service, buffer = ingest

    await service.accept_message(_message("wamid.rls", bsuid="BSUID-rls"), {"e": 1})

    row = await owner_conn.execute(
        text(
            "SELECT count(*) FROM raw_message r JOIN tenant t ON t.id = r.tenant_id "
            "WHERE t.bsuid = 'BSUID-rls'"
        )
    )
    assert row.scalar_one() == 1
    assert len(buffer.pushed) == 1


async def test_a_failed_store_does_not_consume_the_delivery(
    ingest: tuple[Ingest, FakeBuffer], owner_conn: AsyncConnection
) -> None:
    """Deduplication is the unique constraint, claimed only once the row is
    durable. Claiming it in a cache beforehand meant a transient Postgres
    failure lost the workout permanently: Meta's retry found the id already
    claimed and returned without persisting anything.
    """
    service, buffer = ingest
    message = _message("wamid.retry", bsuid="BSUID-retry")

    await service.accept_message(message, {"e": 1})
    await service.accept_message(message, {"e": 1})

    row = await owner_conn.execute(
        text("SELECT count(*) FROM raw_message WHERE wa_message_id = 'wamid.retry'")
    )
    assert row.scalar_one() == 1
    assert len(buffer.pushed) == 1, "the redelivery must not reach the buffer twice"
