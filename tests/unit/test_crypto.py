"""AES-256-GCM column encryption (spec 22.2).

The blob is `version (2, big endian) || nonce (12) || ciphertext+tag`. The
version travels *inside* so decryption never depends on a caller passing the
right one; the `key_version` column exists for the rotation job, and a
disagreement between the two is an error rather than a silent read.

Associated data is mandatory and canonical, so a ciphertext that is byte-for-byte
intact still fails to authenticate in any other row, tenant, column or table.
"""

from __future__ import annotations

import pytest

from fittrack.security.crypto import (
    KEY_BYTES,
    MAX_VERSION,
    MIN_VERSION,
    NONCE_BYTES,
    VERSION_BYTES,
    ColumnCipher,
    DecryptionError,
    Keyring,
    KeyringError,
    column_aad,
    identity_aad,
    version_of,
)

KEY_1 = b"\x01" * 32
KEY_2 = b"\x02" * 32


@pytest.fixture
def keyring() -> Keyring:
    return Keyring(keys={1: KEY_1}, active_version=1)


@pytest.fixture
def cipher(keyring: Keyring) -> ColumnCipher:
    return ColumnCipher(keyring)


@pytest.fixture
def aad() -> bytes:
    return column_aad(tenant_id=1, table="health_report", column="verbatim", row_id=42)


# --------------------------------------------------------------------------- #
# The blob format
# --------------------------------------------------------------------------- #


def test_a_round_trip_returns_the_plaintext(cipher: ColumnCipher, aad: bytes) -> None:
    assert cipher.decrypt(cipher.encrypt(b"o ombro doeu", aad), aad) == b"o ombro doeu"


def test_the_blob_carries_its_version_first(cipher: ColumnCipher, aad: bytes) -> None:
    blob = cipher.encrypt(b"x", aad)
    assert blob[:VERSION_BYTES] == (1).to_bytes(VERSION_BYTES, "big")
    assert version_of(blob) == 1


def test_the_blob_is_version_nonce_and_ciphertext(cipher: ColumnCipher, aad: bytes) -> None:
    blob = cipher.encrypt(b"", aad)
    # Empty plaintext still carries the 16-byte GCM tag.
    assert len(blob) == VERSION_BYTES + NONCE_BYTES + 16


def test_the_ciphertext_does_not_contain_the_plaintext(cipher: ColumnCipher, aad: bytes) -> None:
    secret = b"supino reto 80kg"
    assert secret not in cipher.encrypt(secret, aad)


def test_two_encryptions_of_one_plaintext_differ(cipher: ColumnCipher, aad: bytes) -> None:
    """A random nonce per row: equality on ciphertext must reveal nothing."""
    first = cipher.encrypt(b"same", aad)
    second = cipher.encrypt(b"same", aad)
    assert first != second
    assert cipher.decrypt(first, aad) == cipher.decrypt(second, aad) == b"same"


# --------------------------------------------------------------------------- #
# Failing closed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("index", [0, 1, 2, 5, 14, 15, -1, -8])
def test_flipping_any_byte_fails(cipher: ColumnCipher, aad: bytes, index: int) -> None:
    blob = bytearray(cipher.encrypt(b"o joelho inchou", aad))
    blob[index] ^= 0x01
    with pytest.raises(DecryptionError):
        cipher.decrypt(bytes(blob), aad)


def test_a_truncated_blob_fails(cipher: ColumnCipher, aad: bytes) -> None:
    blob = cipher.encrypt(b"x", aad)
    with pytest.raises(DecryptionError):
        cipher.decrypt(blob[:-1], aad)


@pytest.mark.parametrize("size", [0, 1, VERSION_BYTES, VERSION_BYTES + NONCE_BYTES])
def test_a_blob_too_short_to_be_one_fails(cipher: ColumnCipher, aad: bytes, size: int) -> None:
    with pytest.raises(DecryptionError):
        cipher.decrypt(b"\x00" * size, aad)


def test_an_unknown_version_fails(cipher: ColumnCipher, aad: bytes) -> None:
    blob = bytearray(cipher.encrypt(b"x", aad))
    blob[0:VERSION_BYTES] = (9).to_bytes(VERSION_BYTES, "big")
    with pytest.raises(DecryptionError):
        cipher.decrypt(bytes(blob), aad)


def test_the_wrong_key_fails(aad: bytes) -> None:
    written = ColumnCipher(Keyring(keys={1: KEY_1}, active_version=1)).encrypt(b"x", aad)
    other = ColumnCipher(Keyring(keys={1: KEY_2}, active_version=1))
    with pytest.raises(DecryptionError):
        other.decrypt(written, aad)


def test_a_failure_says_nothing_about_which_part_failed(cipher: ColumnCipher, aad: bytes) -> None:
    """One message for every failure: the difference is an oracle."""
    blob = bytearray(cipher.encrypt(b"x", aad))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError) as tampered:
        cipher.decrypt(bytes(blob), aad)
    with pytest.raises(DecryptionError) as wrong_aad:
        cipher.decrypt(
            cipher.encrypt(b"x", aad),
            column_aad(tenant_id=2, table="health_report", column="verbatim", row_id=42),
        )
    assert str(tampered.value) == str(wrong_aad.value)


# --------------------------------------------------------------------------- #
# Associated data: an intact ciphertext in the wrong place must not decrypt
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "moved",
    [
        {"tenant_id": 2},
        {"table": "body_metric"},
        {"column": "guidance"},
        {"row_id": 43},
    ],
    ids=["another tenant", "another table", "another column", "another row"],
)
def test_moving_an_intact_ciphertext_fails(cipher: ColumnCipher, moved: dict[str, object]) -> None:
    original = {"tenant_id": 1, "table": "health_report", "column": "verbatim", "row_id": 42}
    blob = cipher.encrypt(b"dor lombar", column_aad(**original))  # type: ignore[arg-type]
    with pytest.raises(DecryptionError):
        cipher.decrypt(blob, column_aad(**{**original, **moved}))  # type: ignore[arg-type]


def test_the_column_aad_pins_the_contract_and_every_field() -> None:
    aad = column_aad(tenant_id=7, table="body_metric", column="value", row_id=3)
    for part in (b"fittrack:v1", b"body_metric", b"value", b"tenant:7", b"row:3"):
        assert part in aad


def test_a_separator_cannot_be_smuggled_between_aad_fields() -> None:
    """The same discipline `identity_hash` applies, for the same reason.

    Separator-joined, `table="a|b", column="c"` and `table="a", column="b|c"`
    are one string — and a blob would authenticate across both contexts.
    """
    assert column_aad(tenant_id=1, table="a|b", column="c", row_id=2) != column_aad(
        tenant_id=1, table="a", column="b|c", row_id=2
    )


def test_the_identity_aad_needs_no_tenant() -> None:
    """The pre-tenant exception of 22.2: it is built before the tenant exists.

    The first webhook has only a channel and a hash, and the tenant and its
    identity are created in one transaction — so the AAD cannot name a row id
    that does not exist yet.
    """
    aad = identity_aad(channel="telegram", external_id_hash=b"\xab\xcd")
    for part in (
        b"fittrack:v1",
        b"channel_identity",
        b"external_id",
        b"channel:telegram",
        b"hash:abcd",
    ):
        assert part in aad
    assert b"tenant:" not in aad
    assert b"row:" not in aad


def test_an_identity_blob_does_not_move_between_channels(cipher: ColumnCipher) -> None:
    blob = cipher.encrypt(b"12345", identity_aad(channel="telegram", external_id_hash=b"\x01"))
    with pytest.raises(DecryptionError):
        cipher.decrypt(blob, identity_aad(channel="whatsapp", external_id_hash=b"\x01"))


def test_an_identity_blob_does_not_move_between_hashes(cipher: ColumnCipher) -> None:
    blob = cipher.encrypt(b"12345", identity_aad(channel="telegram", external_id_hash=b"\x01"))
    with pytest.raises(DecryptionError):
        cipher.decrypt(blob, identity_aad(channel="telegram", external_id_hash=b"\x02"))


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #


def test_writes_use_the_active_version_only(aad: bytes) -> None:
    cipher = ColumnCipher(Keyring(keys={1: KEY_1, 2: KEY_2}, active_version=2))
    assert version_of(cipher.encrypt(b"x", aad)) == 2


def test_both_versions_stay_readable_during_a_backfill(aad: bytes) -> None:
    """The point of the whole scheme: no downtime window."""
    old = ColumnCipher(Keyring(keys={1: KEY_1}, active_version=1)).encrypt(b"antigo", aad)
    during = ColumnCipher(Keyring(keys={1: KEY_1, 2: KEY_2}, active_version=2))
    assert during.decrypt(old, aad) == b"antigo"
    assert during.decrypt(during.encrypt(b"novo", aad), aad) == b"novo"


def test_a_declared_version_that_disagrees_with_the_blob_is_an_error(
    cipher: ColumnCipher, aad: bytes
) -> None:
    """A half-finished rotation, not a value to read past (spec 22.2)."""
    blob = cipher.encrypt(b"x", aad)
    with pytest.raises(DecryptionError, match="key_version"):
        cipher.decrypt(blob, aad, declared_version=2)


def test_a_declared_version_that_agrees_is_accepted(cipher: ColumnCipher, aad: bytes) -> None:
    blob = cipher.encrypt(b"x", aad)
    assert cipher.decrypt(blob, aad, declared_version=1) == b"x"


# --------------------------------------------------------------------------- #
# The keyring
# --------------------------------------------------------------------------- #


def test_the_active_version_must_be_in_the_keyring() -> None:
    with pytest.raises(KeyringError):
        Keyring(keys={1: KEY_1}, active_version=2)


@pytest.mark.parametrize("version", [0, -1, 32768])
def test_a_version_outside_the_smallint_range_is_rejected(version: int) -> None:
    with pytest.raises(KeyringError):
        Keyring(keys={version: KEY_1}, active_version=version)


def test_a_key_of_the_wrong_length_is_rejected() -> None:
    with pytest.raises(KeyringError, match="32 bytes"):
        Keyring(keys={1: b"short"}, active_version=1)


def test_an_empty_keyring_is_rejected() -> None:
    with pytest.raises(KeyringError):
        Keyring(keys={}, active_version=1)


def test_a_key_still_in_use_cannot_be_retired() -> None:
    """Removing it would make every row on that version unreadable, forever."""
    keyring = Keyring(keys={1: KEY_1, 2: KEY_2}, active_version=2)
    with pytest.raises(KeyringError, match="still in use"):
        keyring.assert_retirable(1, versions_present={1, 2})


def test_a_key_no_longer_present_may_be_retired() -> None:
    keyring = Keyring(keys={1: KEY_1, 2: KEY_2}, active_version=2)
    keyring.assert_retirable(1, versions_present={2})


def test_the_active_version_may_never_be_retired() -> None:
    keyring = Keyring(keys={1: KEY_1, 2: KEY_2}, active_version=2)
    with pytest.raises(KeyringError, match="active"):
        keyring.assert_retirable(2, versions_present=set())


def test_a_version_present_in_the_database_must_be_in_the_keyring() -> None:
    keyring = Keyring(keys={2: KEY_2}, active_version=2)
    with pytest.raises(KeyringError, match="unreadable"):
        keyring.assert_can_read(versions_present={1, 2})


# --------------------------------------------------------------------------- #
# Nothing leaks
# --------------------------------------------------------------------------- #


def test_the_keyring_does_not_print_its_material(keyring: Keyring) -> None:
    assert "01010101" not in repr(keyring)
    assert str(KEY_1) not in repr(keyring)


def test_a_keyring_error_does_not_quote_the_key() -> None:
    with pytest.raises(KeyringError) as raised:
        Keyring(keys={1: b"A" * 31}, active_version=1)
    assert "AAAA" not in str(raised.value)


def test_a_decryption_error_does_not_quote_the_blob(cipher: ColumnCipher, aad: bytes) -> None:
    blob = bytearray(cipher.encrypt(b"x", aad))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError) as raised:
        cipher.decrypt(bytes(blob), aad)
    assert bytes(blob).hex()[:16] not in str(raised.value)


# --------------------------------------------------------------------------- #
# From settings
# --------------------------------------------------------------------------- #


def test_the_keyring_is_built_from_validated_settings() -> None:
    """Settings already parse and bound-check the keyring; this must not re-decide."""
    import base64
    import json

    from fittrack.settings import Settings

    keys = {"1": base64.b64encode(KEY_1).decode(), "2": base64.b64encode(KEY_2).decode()}
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://fittrack_runtime:p@postgres:5432/f?sslmode=verify-full",
        REDIS_URL="rediss://:p@redis:6379/0",
        QDRANT_URL="https://qdrant:6333",
        FITTRACK_ENCRYPTION_KEYS=json.dumps(keys),
        FITTRACK_ACTIVE_KEY_VERSION="2",
        FITTRACK_IDENTITY_PEPPER="an-independent-pepper-long-enough-for-the-rule",
    )

    keyring = Keyring.from_settings(settings)
    assert keyring.versions == {1, 2}
    assert keyring.active_version == 2


# --------------------------------------------------------------------------- #
# Associated data is mandatory, not merely documented
# --------------------------------------------------------------------------- #


def test_encrypting_without_associated_data_is_refused(cipher: ColumnCipher) -> None:
    """A blob bound to nothing decrypts in every context and fails no test."""
    with pytest.raises(ValueError, match="mandatory"):
        cipher.encrypt(b"x", b"")


def test_decrypting_without_associated_data_is_refused(cipher: ColumnCipher, aad: bytes) -> None:
    blob = cipher.encrypt(b"x", aad)
    with pytest.raises(ValueError, match="mandatory"):
        cipher.decrypt(blob, b"")


def test_the_bounds_come_from_one_place() -> None:
    """Two copies let a widened bound pass boot and throw at first use."""
    from fittrack import settings

    assert KEY_BYTES == settings.KEY_BYTES
    assert MIN_VERSION == settings.MIN_KEY_VERSION
    assert MAX_VERSION == settings.MAX_KEY_VERSION
