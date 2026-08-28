"""The encrypted columns, against the real database (spec 22.2).

Two things the unit tests cannot show: that a ciphertext survives a round trip
through `BYTEA`, and that the three consequences the spec calls out are actually
true of this schema — a randomised ciphertext is not searchable, not aggregable,
and an intact blob moved to another row does not decrypt.
"""

from __future__ import annotations

import secrets

import asyncpg
import pytest

from fittrack.security.crypto import ColumnCipher, DecryptionError, Keyring, column_aad
from fittrack.security.identity_hash import identity_hash

PEPPER = b"an-integration-pepper-of-sufficient-length"


@pytest.fixture
def cipher() -> ColumnCipher:
    return ColumnCipher(Keyring(keys={1: b"\x11" * 32}, active_version=1))


async def make_tenant(owner: asyncpg.Connection, name: str) -> int:
    tenant_id: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ($1) RETURNING id", name
    )
    return tenant_id


async def test_an_encrypted_column_survives_the_round_trip(
    owner: asyncpg.Connection, cipher: ColumnCipher
) -> None:
    """The row id is reserved before encrypting, because the AAD names it."""
    tenant = await make_tenant(owner, "round-trip")
    verbatim = "dor no ombro direito depois do desenvolvimento"

    # Reserve the id first: the AAD binds the blob to this row, so it cannot be
    # built until the row has an identity (spec 22.2).
    row_id: int = await owner.fetchval("SELECT nextval('health_report_id_seq')")
    aad = column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=row_id)
    await owner.execute(
        """
        INSERT INTO health_report (id, tenant_id, category, verbatim, key_version)
        VALUES ($1, $2, 'dor', $3, 1)
        """,
        row_id,
        tenant,
        cipher.encrypt(verbatim.encode(), aad),
    )

    stored = await owner.fetchrow(
        "SELECT verbatim, key_version FROM health_report WHERE id = $1", row_id
    )
    assert cipher.decrypt(stored["verbatim"], aad, stored["key_version"]).decode() == verbatim


async def test_the_database_never_holds_the_plaintext(
    owner: asyncpg.Connection, cipher: ColumnCipher
) -> None:
    tenant = await make_tenant(owner, "opaque")
    verbatim = f"marcador-{secrets.token_hex(6)}"
    row_id: int = await owner.fetchval("SELECT nextval('health_report_id_seq')")
    aad = column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=row_id)
    await owner.execute(
        """
        INSERT INTO health_report (id, tenant_id, category, verbatim)
        VALUES ($1, $2, 'dor', $3)
        """,
        row_id,
        tenant,
        cipher.encrypt(verbatim.encode(), aad),
    )

    # The marker must not appear anywhere in the column, under any encoding.
    assert not await owner.fetchval(
        "SELECT count(*) FROM health_report WHERE position($1::bytea in verbatim) > 0",
        verbatim.encode(),
    )


async def test_a_ciphertext_is_not_searchable(
    owner: asyncpg.Connection, cipher: ColumnCipher
) -> None:
    """Spec 22.2, consequence 1: the nonce is per row, so not even equality works."""
    tenant = await make_tenant(owner, "unsearchable")
    aads = []
    for _ in range(2):
        row_id: int = await owner.fetchval("SELECT nextval('health_report_id_seq')")
        aad = column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=row_id)
        aads.append(aad)
        await owner.execute(
            """
            INSERT INTO health_report (id, tenant_id, category, verbatim)
            VALUES ($1, $2, 'dor', $3)
            """,
            row_id,
            tenant,
            cipher.encrypt(b"a mesma dor", aad),
        )

    rows = await owner.fetch(
        "SELECT verbatim FROM health_report WHERE tenant_id = $1 ORDER BY id", tenant
    )
    assert rows[0]["verbatim"] != rows[1]["verbatim"], "identical plaintexts share a ciphertext"
    # Both still decrypt, each under its own row's associated data.
    assert cipher.decrypt(rows[0]["verbatim"], aads[0]) == b"a mesma dor"
    assert cipher.decrypt(rows[1]["verbatim"], aads[1]) == b"a mesma dor"


async def test_a_ciphertext_is_not_aggregable(
    owner: asyncpg.Connection, cipher: ColumnCipher
) -> None:
    """Spec 22.2, consequence 1: `body_metric_trend` aggregates in Python, not SQL."""
    tenant = await make_tenant(owner, "unaggregable")
    row_id: int = await owner.fetchval("SELECT nextval('body_metric_id_seq')")
    aad = column_aad(tenant_id=tenant, table="body_metric", column="value", row_id=row_id)
    await owner.execute(
        """
        INSERT INTO body_metric (id, tenant_id, local_date, kind, value, unit)
        VALUES ($1, $2, CURRENT_DATE, 'peso', $3, 'kg')
        """,
        row_id,
        tenant,
        cipher.encrypt(b"84.9", aad),
    )

    with pytest.raises(asyncpg.PostgresError):
        await owner.fetchval(
            "SELECT avg(value::numeric) FROM body_metric WHERE tenant_id = $1", tenant
        )


async def test_no_index_can_help_a_lookup_by_value(owner: asyncpg.Connection) -> None:
    """Spec 22.2, consequence 3: an index on a randomised ciphertext is dead weight."""
    indexed = await owner.fetch(
        """
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename IN ('health_report', 'body_metric')
           AND indexdef ~ '\\m(verbatim|value)\\M'
        """
    )
    assert not indexed


async def test_a_blob_moved_to_another_row_does_not_decrypt(
    owner: asyncpg.Connection, cipher: ColumnCipher
) -> None:
    """The AAD is what makes a database dump plus write access insufficient."""
    tenant = await make_tenant(owner, "moved")
    first: int = await owner.fetchval("SELECT nextval('health_report_id_seq')")
    second: int = await owner.fetchval("SELECT nextval('health_report_id_seq')")
    blob = cipher.encrypt(
        b"relato do paciente",
        column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=first),
    )

    # Write the *intact* blob into the other row, as an attacker with write
    # access to the dump would.
    await owner.execute(
        """
        INSERT INTO health_report (id, tenant_id, category, verbatim)
        VALUES ($1, $2, 'dor', $3)
        """,
        second,
        tenant,
        blob,
    )
    stored = await owner.fetchval("SELECT verbatim FROM health_report WHERE id = $1", second)
    with pytest.raises(DecryptionError):
        cipher.decrypt(
            stored,
            column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=second),
        )


async def test_the_identity_hash_finds_the_row_the_ciphertext_cannot(
    owner: asyncpg.Connection, cipher: ColumnCipher
) -> None:
    """The whole reason the hash column exists (spec 22.2)."""
    from fittrack.security.crypto import identity_aad

    tenant = await make_tenant(owner, "lookup")
    external_id = f"chat-{secrets.token_hex(6)}"
    digest = identity_hash("telegram", external_id, PEPPER)
    await owner.execute(
        """
        INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)
        VALUES ($1, 'telegram', $2, $3)
        """,
        tenant,
        cipher.encrypt(
            external_id.encode(), identity_aad(channel="telegram", external_id_hash=digest)
        ),
        digest,
    )

    found = await owner.fetchval(
        """
        SELECT tenant_id FROM channel_identity
         WHERE channel = 'telegram' AND external_id_hash = $1 AND revoked_at IS NULL
        """,
        digest,
    )
    assert found == tenant


# --------------------------------------------------------------------------- #
# Key rotation: progressive, and reversible only in one direction
# --------------------------------------------------------------------------- #

KEY_1 = b"\x11" * 32
KEY_2 = b"\x33" * 32


async def versions_present(owner: asyncpg.Connection, tenant: int) -> set[int]:
    """What `key_version` is what it is for: finding rows still to rewrite."""
    rows = await owner.fetch(
        "SELECT DISTINCT key_version FROM health_report WHERE tenant_id = $1", tenant
    )
    return {row["key_version"] for row in rows}


async def write_report(
    owner: asyncpg.Connection, cipher: ColumnCipher, tenant: int, text: bytes
) -> int:
    row_id: int = await owner.fetchval("SELECT nextval('health_report_id_seq')")
    aad = column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=row_id)
    await owner.execute(
        """
        INSERT INTO health_report (id, tenant_id, category, verbatim, key_version)
        VALUES ($1, $2, 'dor', $3, $4)
        """,
        row_id,
        tenant,
        cipher.encrypt(text, aad),
        cipher.active_version,
    )
    return row_id


async def test_a_key_in_use_cannot_be_retired_and_then_can(
    owner: asyncpg.Connection,
) -> None:
    """The backfill, end to end: no downtime, and no early removal.

    Dropping a key while rows still carry its version does not degrade — it
    makes those rows permanently unreadable, so the guard is the whole safety.
    """
    from fittrack.security.crypto import KeyringError

    tenant = await make_tenant(owner, "key-rotation")
    old_only = ColumnCipher(Keyring(keys={1: KEY_1}, active_version=1))
    row_id = await write_report(owner, old_only, tenant, b"relato antigo")
    assert await versions_present(owner, tenant) == {1}

    # Deploy the new key. Writes move to version 2; reads must still serve 1.
    during = Keyring(keys={1: KEY_1, 2: KEY_2}, active_version=2)
    during.assert_can_read(await versions_present(owner, tenant))
    with pytest.raises(KeyringError, match="still in use"):
        during.assert_retirable(1, await versions_present(owner, tenant))

    rotating = ColumnCipher(during)
    stored = await owner.fetchrow(
        "SELECT verbatim, key_version FROM health_report WHERE id = $1", row_id
    )
    aad = column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=row_id)
    plaintext = rotating.decrypt(stored["verbatim"], aad, stored["key_version"])
    assert plaintext == b"relato antigo"

    # The maintenance job's single row: re-encrypt under the active key and
    # move `key_version` with it, in one statement.
    await owner.execute(
        "UPDATE health_report SET verbatim = $2, key_version = $3 WHERE id = $1",
        row_id,
        rotating.encrypt(plaintext, aad),
        rotating.active_version,
    )

    assert await versions_present(owner, tenant) == {2}
    during.assert_retirable(1, await versions_present(owner, tenant))

    # And with the old key gone, the row is still readable.
    after = Keyring(keys={2: KEY_2}, active_version=2)
    after.assert_can_read(await versions_present(owner, tenant))
    row = await owner.fetchrow(
        "SELECT verbatim, key_version FROM health_report WHERE id = $1", row_id
    )
    assert ColumnCipher(after).decrypt(row["verbatim"], aad, row["key_version"]) == plaintext


async def test_a_stale_key_version_column_is_caught(owner: asyncpg.Connection) -> None:
    """A backfill that updated the blob and not the column, or the reverse."""
    tenant = await make_tenant(owner, "half-rotated")
    keyring = Keyring(keys={1: KEY_1, 2: KEY_2}, active_version=1)
    cipher = ColumnCipher(keyring)
    row_id = await write_report(owner, cipher, tenant, b"relato")

    await owner.execute("UPDATE health_report SET key_version = 2 WHERE id = $1", row_id)

    stored = await owner.fetchrow(
        "SELECT verbatim, key_version FROM health_report WHERE id = $1", row_id
    )
    aad = column_aad(tenant_id=tenant, table="health_report", column="verbatim", row_id=row_id)
    with pytest.raises(DecryptionError, match="rotation did not finish"):
        cipher.decrypt(stored["verbatim"], aad, stored["key_version"])
