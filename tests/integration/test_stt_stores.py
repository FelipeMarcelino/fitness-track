"""The two PostgreSQL stores behind voice ingestion (S02-T07, spec 11.3, 22.2).

They are what the acceptance criteria of the task actually rest on and the part
the unit suite cannot reach: `SqlConsentGate` decides whether a recording is
transcribed at all, and `SqlTranscriptStore` is the encrypted column that keeps
a batch retry from paying for the same transcription twice.

The consent query is the subtle one. The `consent` table keeps history rather
than a flag, so "does this tenant consent" is a question about the *latest*
record — and getting the order of the filter and the ordering wrong turns a
revoked consent into a granted one, silently.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fittrack.db.engine import split_ssl_arguments
from fittrack.security.crypto import ColumnCipher, Keyring
from fittrack.services.stt import (
    WORKOUT_DATA_CONSENT,
    SqlConsentGate,
    SqlTranscriptStore,
    encrypt_transcript,
)
from tests.conftest import CA_FILE

KEY = b"\x31" * 32
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def cipher() -> ColumnCipher:
    return ColumnCipher(Keyring(keys={1: KEY}, active_version=1))


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


async def make_tenant(owner: asyncpg.Connection) -> int:
    tenant_id: int = await owner.fetchval("INSERT INTO tenant DEFAULT VALUES RETURNING id")
    return tenant_id


async def make_identity(owner: asyncpg.Connection, tenant_id: int) -> int:
    # A fresh digest per run: `channel_identity` has a unique index on the
    # active pair, and a fixed one makes the test pass once per database.
    identity_id: int = await owner.fetchval(
        "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash) "
        "VALUES ($1, 'telegram', $2, $3) RETURNING id",
        tenant_id,
        b"sealed-by-fixture",
        secrets.token_bytes(16),
    )
    return identity_id


async def make_voice_message(
    owner: asyncpg.Connection, tenant_id: int, *, identity_id: int | None = None
) -> int:
    # One identity per tenant: `ux_channel_identity_primary` allows a single
    # primary identity, so a second message for the same tenant reuses it.
    identity_id = identity_id or await make_identity(owner, tenant_id)
    raw_message_id: int = await owner.fetchval(
        "INSERT INTO raw_message ("
        "tenant_id, identity_id, channel, channel_message_id, direction, msg_type, payload"
        ") VALUES ($1, $2, 'telegram', $3, 'inbound', 'voice', $4) RETURNING id",
        tenant_id,
        identity_id,
        secrets.token_hex(8),
        b"sealed-by-fixture",
    )
    return raw_message_id


async def record_consent(
    owner: asyncpg.Connection,
    tenant_id: int,
    *,
    granted: bool,
    at: datetime,
    revoked_at: datetime | None = None,
    kind: str = WORKOUT_DATA_CONSENT,
) -> None:
    await owner.execute(
        "INSERT INTO consent (tenant_id, kind, granted, text_hash, version, granted_at, revoked_at)"
        " VALUES ($1, $2::consent_kind, $3, 'sha256:fixture', 'privacy-2026-08', $4, $5)",
        tenant_id,
        kind,
        granted,
        at,
        revoked_at,
    )


# --------------------------------------------------------------------------- #
# SqlConsentGate (spec 11.3, 19.5) — acceptance criterion 9
# --------------------------------------------------------------------------- #


async def test_a_granted_consent_opens_the_gate(
    owner: asyncpg.Connection, sessions: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await make_tenant(owner)
    await record_consent(owner, tenant_id, granted=True, at=NOW)

    assert await SqlConsentGate(sessions).has_consent(
        tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT
    )


async def test_a_tenant_that_never_consented_is_refused(
    owner: asyncpg.Connection, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Fail closed: no record is not a yes."""
    tenant_id = await make_tenant(owner)

    assert not await SqlConsentGate(sessions).has_consent(
        tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT
    )


async def test_a_revocation_on_the_latest_record_closes_the_gate(
    owner: asyncpg.Connection, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The bug this query was rewritten for.

    Filtering `revoked_at IS NULL` before ordering drops the revocation and
    answers from the older grant sitting behind it — a revoked consent reading
    as granted, which is the one failure mode of §11.3 that matters.
    """
    tenant_id = await make_tenant(owner)
    await record_consent(owner, tenant_id, granted=True, at=NOW - timedelta(days=30))
    await record_consent(
        owner, tenant_id, granted=True, at=NOW, revoked_at=NOW + timedelta(minutes=1)
    )

    assert not await SqlConsentGate(sessions).has_consent(
        tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT
    )


async def test_a_later_refusal_closes_the_gate(
    owner: asyncpg.Connection, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The other shape of a revocation: a newer row that says no."""
    tenant_id = await make_tenant(owner)
    await record_consent(owner, tenant_id, granted=True, at=NOW - timedelta(days=30))
    await record_consent(owner, tenant_id, granted=False, at=NOW)

    assert not await SqlConsentGate(sessions).has_consent(
        tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT
    )


async def test_consent_granted_again_after_a_revocation_reopens_the_gate(
    owner: asyncpg.Connection, sessions: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await make_tenant(owner)
    await record_consent(owner, tenant_id, granted=False, at=NOW - timedelta(days=1))
    await record_consent(owner, tenant_id, granted=True, at=NOW)

    assert await SqlConsentGate(sessions).has_consent(
        tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT
    )


async def test_another_kind_of_consent_does_not_open_this_gate(
    owner: asyncpg.Connection, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`workout_data` covers the recording; `health_data` is a separate yes (19.5)."""
    tenant_id = await make_tenant(owner)
    await record_consent(owner, tenant_id, granted=True, at=NOW, kind="health_data")

    assert not await SqlConsentGate(sessions).has_consent(
        tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT
    )


async def test_one_tenants_consent_never_answers_for_another(
    owner: asyncpg.Connection, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Invariant 3 and §19.1: the query carries no tenant, the session does."""
    consenting = await make_tenant(owner)
    silent = await make_tenant(owner)
    await record_consent(owner, consenting, granted=True, at=NOW)

    gate = SqlConsentGate(sessions)

    assert await gate.has_consent(tenant_id=consenting, kind=WORKOUT_DATA_CONSENT)
    assert not await gate.has_consent(tenant_id=silent, kind=WORKOUT_DATA_CONSENT)


# --------------------------------------------------------------------------- #
# SqlTranscriptStore (spec 22.2) — acceptance criteria 6 and 7
# --------------------------------------------------------------------------- #


async def test_a_transcript_round_trips_and_reaches_postgres_encrypted(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    tenant_id = await make_tenant(owner)
    raw_message_id = await make_voice_message(owner, tenant_id)
    store = SqlTranscriptStore(sessions, cipher)
    words = "supino reto dez quilos oito repeticoes"

    await store.save(tenant_id=tenant_id, raw_message_id=raw_message_id, transcript=words)

    assert await store.load_transcript(tenant_id=tenant_id, raw_message_id=raw_message_id) == words
    stored = await owner.fetchval(
        "SELECT transcript FROM raw_message WHERE id = $1", raw_message_id
    )
    assert isinstance(stored, bytes)
    assert words.encode() not in stored, "the database can see the user's words"


async def test_a_row_without_a_transcript_answers_none(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    tenant_id = await make_tenant(owner)
    raw_message_id = await make_voice_message(owner, tenant_id)

    store = SqlTranscriptStore(sessions, cipher)

    assert await store.load_transcript(tenant_id=tenant_id, raw_message_id=raw_message_id) is None


async def test_a_transcript_that_no_longer_opens_answers_none(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    """A retired key must not wedge the tenant's pipeline (B-1).

    The drain is kept until its batch is persisted (§17.3) and the gated drain
    will not rename the buffer while an orphan exists, so an exception here
    would strand every later message of that tenant — text included — behind
    one unreadable blob. Answering "no transcript" costs one re-transcription.
    """
    tenant_id = await make_tenant(owner)
    raw_message_id = await make_voice_message(owner, tenant_id)
    retired = ColumnCipher(Keyring(keys={1: b"\x99" * 32}, active_version=1))
    await owner.execute(
        "UPDATE raw_message SET transcript = $1 WHERE id = $2",
        encrypt_transcript(
            retired, tenant_id=tenant_id, raw_message_id=raw_message_id, text="ilegivel"
        ),
        raw_message_id,
    )

    store = SqlTranscriptStore(sessions, cipher)

    assert await store.load_transcript(tenant_id=tenant_id, raw_message_id=raw_message_id) is None


async def test_a_transcript_does_not_open_against_another_row(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    """Spec 22.2: the associated data binds the blob to its tenant and row.

    This is what makes answering `None` above safe rather than lax — a blob
    that fails here cannot be succeeding as some other row's transcript.
    """
    tenant_id = await make_tenant(owner)
    identity_id = await make_identity(owner, tenant_id)
    mine = await make_voice_message(owner, tenant_id, identity_id=identity_id)
    theirs = await make_voice_message(owner, tenant_id, identity_id=identity_id)
    store = SqlTranscriptStore(sessions, cipher)
    await store.save(tenant_id=tenant_id, raw_message_id=mine, transcript="meu treino")

    moved = await owner.fetchval("SELECT transcript FROM raw_message WHERE id = $1", mine)
    await owner.execute("UPDATE raw_message SET transcript = $1 WHERE id = $2", moved, theirs)

    assert await store.load_transcript(tenant_id=tenant_id, raw_message_id=theirs) is None
    assert await store.load_transcript(tenant_id=tenant_id, raw_message_id=mine) == "meu treino"


async def test_load_reports_the_identity_the_reply_goes_to(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    tenant_id = await make_tenant(owner)
    raw_message_id = await make_voice_message(owner, tenant_id)
    expected = await owner.fetchval(
        "SELECT identity_id FROM raw_message WHERE id = $1", raw_message_id
    )

    row = await SqlTranscriptStore(sessions, cipher).load(
        tenant_id=tenant_id, raw_message_id=raw_message_id
    )

    assert row is not None
    assert row.identity_id == expected
    assert row.channel == "telegram"
    assert row.answered is False


async def test_a_row_that_is_gone_loads_as_none(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    tenant_id = await make_tenant(owner)

    assert (
        await SqlTranscriptStore(sessions, cipher).load(
            tenant_id=tenant_id, raw_message_id=2_000_000_000
        )
        is None
    )


async def test_answering_a_recording_is_recorded_once(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    """`answered_at` is the durable marker that stops a duplicate fixed reply."""
    tenant_id = await make_tenant(owner)
    raw_message_id = await make_voice_message(owner, tenant_id)
    store = SqlTranscriptStore(sessions, cipher)

    await store.mark_answered(tenant_id=tenant_id, raw_message_id=raw_message_id)
    first = await owner.fetchval(
        "SELECT answered_at FROM raw_message WHERE id = $1", raw_message_id
    )
    await store.mark_answered(tenant_id=tenant_id, raw_message_id=raw_message_id)
    second = await owner.fetchval(
        "SELECT answered_at FROM raw_message WHERE id = $1", raw_message_id
    )

    assert first is not None
    assert second == first, "a second answer moved the marker"
    row = await store.load(tenant_id=tenant_id, raw_message_id=raw_message_id)
    assert row is not None
    assert row.answered is True


async def test_a_stored_transcription_is_not_an_answer_to_the_user(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    """D-3. The two facts are separate, and one used to stand for the other.

    `save` recording a transcription must not make `load` report the message as
    answered. It did, through a shared `processed_at`, and `_refuse` reads that
    flag: a transcription that succeeded and then failed to persist would, on
    the retried drain with consent revoked in between, suppress the consent
    reply *and* drop the item — leaving the user with nothing at all.
    """
    tenant_id = await make_tenant(owner)
    raw_message_id = await make_voice_message(owner, tenant_id)
    store = SqlTranscriptStore(sessions, cipher)

    await store.save(
        tenant_id=tenant_id, raw_message_id=raw_message_id, transcript="supino dez quilos"
    )
    row = await store.load(tenant_id=tenant_id, raw_message_id=raw_message_id)

    assert row is not None
    assert row.answered is False, "a transcription was recorded as an answer to the user"


async def test_processing_a_message_is_not_answering_it_either(
    owner: asyncpg.Connection,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
) -> None:
    """`processed_at` belongs to the graph run of Sprint 03, not to this flag.

    Sharing the column would have made the graph's own bookkeeping suppress a
    fixed reply months from now, in a place nobody would think to look.
    """
    tenant_id = await make_tenant(owner)
    raw_message_id = await make_voice_message(owner, tenant_id)
    await owner.execute("UPDATE raw_message SET processed_at = now() WHERE id = $1", raw_message_id)

    row = await SqlTranscriptStore(sessions, cipher).load(
        tenant_id=tenant_id, raw_message_id=raw_message_id
    )

    assert row is not None
    assert row.answered is False
