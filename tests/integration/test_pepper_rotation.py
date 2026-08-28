"""Rotating the identity pepper (spec 22.2).

It cannot be a dual-read rotation. Hashes computed under two peppers would both
escape `ux_channel_identity_active`, so the same account could resolve to two
tenants — the one failure this table exists to prevent. It is therefore an
atomic maintenance: lock the table, decrypt under the old associated data,
rehash, re-encrypt under the new associated data, and commit. The secret is
swapped only after the commit, and a failure rolls back before it.
"""

from __future__ import annotations

import secrets

import asyncpg
import pytest

from fittrack.security.crypto import ColumnCipher, Keyring, identity_aad
from fittrack.security.identity_hash import identity_hash
from fittrack.security.rotation import PepperRotation, rotate_pepper

OLD_PEPPER = b"the-old-identity-pepper-long-enough-ok"
NEW_PEPPER = b"the-new-identity-pepper-long-enough-ok"


@pytest.fixture
def cipher() -> ColumnCipher:
    return ColumnCipher(Keyring(keys={1: b"\x22" * 32}, active_version=1))


@pytest.fixture
async def empty_identities(owner: asyncpg.Connection) -> None:
    """Rotation is table-wide, so the table has to start as this test's alone.

    The database persists between tests and other modules leave identities
    behind, encrypted under their own keys — which `rotate_pepper` would
    correctly refuse to decrypt. That refusal is the behaviour under test
    elsewhere; here it is just noise.
    """
    await owner.execute("TRUNCATE channel_identity CASCADE")


async def seed_identity(
    owner: asyncpg.Connection, cipher: ColumnCipher, channel: str, external_id: str
) -> tuple[int, int]:
    """A tenant and its identity, written under the old pepper."""
    tenant: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ('rotating') RETURNING id"
    )
    digest = identity_hash(channel, external_id, OLD_PEPPER)
    identity: int = await owner.fetchval(
        """
        INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)
        VALUES ($1, $2::channel_kind, $3, $4) RETURNING id
        """,
        tenant,
        channel,
        cipher.encrypt(
            external_id.encode(), identity_aad(channel=channel, external_id_hash=digest)
        ),
        digest,
    )
    return tenant, identity


async def test_every_lookup_still_resolves_after_rotation(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    external_id = f"chat-{secrets.token_hex(6)}"
    tenant, _ = await seed_identity(owner, cipher, "telegram", external_id)

    result = await rotate_pepper(owner, cipher=cipher, old_pepper=OLD_PEPPER, new_pepper=NEW_PEPPER)
    assert result.rewritten >= 1

    # The old hash finds nothing; the new one finds the same tenant.
    assert not await owner.fetchval(
        "SELECT tenant_id FROM channel_identity WHERE external_id_hash = $1",
        identity_hash("telegram", external_id, OLD_PEPPER),
    )
    assert (
        await owner.fetchval(
            "SELECT tenant_id FROM channel_identity WHERE external_id_hash = $1",
            identity_hash("telegram", external_id, NEW_PEPPER),
        )
        == tenant
    )


async def test_the_identifier_is_still_readable_after_rotation(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    """Re-encrypted under the new AAD, because the hash is part of it."""
    external_id = f"chat-{secrets.token_hex(6)}"
    await seed_identity(owner, cipher, "telegram", external_id)
    await rotate_pepper(owner, cipher=cipher, old_pepper=OLD_PEPPER, new_pepper=NEW_PEPPER)

    row = await owner.fetchrow(
        "SELECT external_id, external_id_hash FROM channel_identity WHERE external_id_hash = $1",
        identity_hash("telegram", external_id, NEW_PEPPER),
    )
    decrypted = cipher.decrypt(
        row["external_id"],
        identity_aad(channel="telegram", external_id_hash=row["external_id_hash"]),
    )
    assert decrypted.decode() == external_id


async def test_rotation_creates_no_duplicate_identity(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    """`ux_channel_identity_active` must hold throughout, not just at the end."""
    external_id = f"chat-{secrets.token_hex(6)}"
    await seed_identity(owner, cipher, "telegram", external_id)
    before = await owner.fetchval("SELECT count(*) FROM channel_identity")

    await rotate_pepper(owner, cipher=cipher, old_pepper=OLD_PEPPER, new_pepper=NEW_PEPPER)

    assert await owner.fetchval("SELECT count(*) FROM channel_identity") == before


async def test_rotation_is_idempotent_under_the_new_pepper(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    """Re-running with the same pair must not corrupt rows already rotated."""
    external_id = f"chat-{secrets.token_hex(6)}"
    await seed_identity(owner, cipher, "telegram", external_id)
    await rotate_pepper(owner, cipher=cipher, old_pepper=OLD_PEPPER, new_pepper=NEW_PEPPER)

    second = await rotate_pepper(owner, cipher=cipher, old_pepper=NEW_PEPPER, new_pepper=NEW_PEPPER)
    assert second.rewritten >= 1
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM channel_identity WHERE external_id_hash = $1",
            identity_hash("telegram", external_id, NEW_PEPPER),
        )
        == 1
    )


async def test_a_failure_rolls_back_before_the_secret_is_swapped(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    """The ordering the spec insists on: commit, then swap; never the reverse.

    A row that cannot be decrypted — the wrong pepper, a half-applied earlier
    run — must leave the table exactly as it was, so retrying is safe.
    """
    external_id = f"chat-{secrets.token_hex(6)}"
    await seed_identity(owner, cipher, "telegram", external_id)
    original = await owner.fetchrow(
        "SELECT external_id, external_id_hash FROM channel_identity WHERE external_id_hash = $1",
        identity_hash("telegram", external_id, OLD_PEPPER),
    )

    from fittrack.security.rotation import RotationError

    wrong = b"a-pepper-that-was-never-used-here-ok!"
    with pytest.raises(RotationError, match="did not finish"):
        await rotate_pepper(owner, cipher=cipher, old_pepper=wrong, new_pepper=NEW_PEPPER)

    after = await owner.fetchrow(
        "SELECT external_id, external_id_hash FROM channel_identity WHERE external_id_hash = $1",
        identity_hash("telegram", external_id, OLD_PEPPER),
    )
    assert after is not None, "the row was rewritten despite the failure"
    assert bytes(after["external_id"]) == bytes(original["external_id"])


async def test_a_revoked_identity_is_rotated_too(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    """Revoked rows keep their history and stay decryptable (LGPD, spec 19.5)."""
    external_id = f"chat-{secrets.token_hex(6)}"
    _, identity = await seed_identity(owner, cipher, "telegram", external_id)
    await owner.execute(
        "UPDATE channel_identity SET revoked_at = now(), is_primary = false WHERE id = $1",
        identity,
    )

    await rotate_pepper(owner, cipher=cipher, old_pepper=OLD_PEPPER, new_pepper=NEW_PEPPER)

    row = await owner.fetchrow(
        "SELECT external_id, external_id_hash FROM channel_identity WHERE id = $1", identity
    )
    assert bytes(row["external_id_hash"]) == identity_hash("telegram", external_id, NEW_PEPPER)


async def test_the_result_reports_what_it_did(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    await seed_identity(owner, cipher, "telegram", f"chat-{secrets.token_hex(6)}")
    result = await rotate_pepper(owner, cipher=cipher, old_pepper=OLD_PEPPER, new_pepper=NEW_PEPPER)
    assert isinstance(result, PepperRotation)
    assert result.rewritten == result.scanned


async def test_the_rotation_refuses_a_short_new_pepper(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    """Checked before the table is locked, not after."""
    from fittrack.security.identity_hash import PepperError

    with pytest.raises(PepperError):
        await rotate_pepper(owner, cipher=cipher, old_pepper=OLD_PEPPER, new_pepper=b"short")


async def test_a_row_encrypted_under_another_key_stops_the_rotation(
    owner: asyncpg.Connection, cipher: ColumnCipher, empty_identities: None
) -> None:
    """Fails closed and rolls back, rather than rewriting what it cannot read."""
    from fittrack.security.crypto import DecryptionError

    external_id = f"chat-{secrets.token_hex(6)}"
    await seed_identity(owner, cipher, "telegram", external_id)

    stranger = ColumnCipher(Keyring(keys={1: b"\x99" * 32}, active_version=1))
    with pytest.raises(DecryptionError):
        await rotate_pepper(owner, cipher=stranger, old_pepper=OLD_PEPPER, new_pepper=NEW_PEPPER)

    # Untouched: the old hash still finds it.
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM channel_identity WHERE external_id_hash = $1",
            identity_hash("telegram", external_id, OLD_PEPPER),
        )
        == 1
    )
