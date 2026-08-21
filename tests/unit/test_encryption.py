"""AES-256-GCM over the columns in §22.2.

The properties that matter are not "it round-trips" -- that is table stakes --
but that ciphertext is non-deterministic, that a wrong key fails loudly instead
of returning garbage, and that a key rotation does not orphan old rows.
"""

from __future__ import annotations

import os

import pytest

from fittrack.crypto.aesgcm import (
    DecryptionError,
    Encryptor,
    KeyRing,
)

KEY_V1 = os.urandom(32)
KEY_V2 = os.urandom(32)


@pytest.fixture
def encryptor() -> Encryptor:
    return Encryptor(KeyRing({1: KEY_V1}, current_version=1))


def test_round_trip(encryptor: Encryptor) -> None:
    blob, version = encryptor.encrypt("dor no ombro direito")

    assert encryptor.decrypt(blob, version) == "dor no ombro direito"


def test_ciphertext_is_not_deterministic(encryptor: Encryptor) -> None:
    """A fresh nonce per row. Deterministic ciphertext would let anyone with
    read access tell which rows share a value -- how many users reported the
    same injury, for instance -- without decrypting anything."""
    first, _ = encryptor.encrypt("mesmo texto")
    second, _ = encryptor.encrypt("mesmo texto")

    assert first != second


def test_wrong_key_raises_rather_than_returning_garbage(
    encryptor: Encryptor,
) -> None:
    """GCM authenticates. Silent garbage would propagate into analyses."""
    blob, version = encryptor.encrypt("segredo")
    other = Encryptor(KeyRing({1: os.urandom(32)}, current_version=1))

    with pytest.raises(DecryptionError):
        other.decrypt(blob, version)


def test_tampered_ciphertext_is_rejected(encryptor: Encryptor) -> None:
    blob, version = encryptor.encrypt("segredo")
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01

    with pytest.raises(DecryptionError):
        encryptor.decrypt(bytes(tampered), version)


def test_rotation_keeps_old_rows_readable() -> None:
    """§22.2 rotates progressively: new writes use the new key while a job
    rewrites history. Old rows must stay readable throughout, or rotation
    becomes an outage."""
    old = Encryptor(KeyRing({1: KEY_V1}, current_version=1))
    blob, version = old.encrypt("escrito antes da rotacao")

    rotated = Encryptor(KeyRing({1: KEY_V1, 2: KEY_V2}, current_version=2))

    assert rotated.decrypt(blob, version) == "escrito antes da rotacao"
    assert rotated.encrypt("novo")[1] == 2


def test_unknown_key_version_raises(encryptor: Encryptor) -> None:
    blob, _ = encryptor.encrypt("x")

    with pytest.raises(DecryptionError, match="key version"):
        encryptor.decrypt(blob, 99)


def test_empty_string_survives(encryptor: Encryptor) -> None:
    """An empty note is different from a missing one, and NULL is how the
    schema says 'missing'."""
    blob, version = encryptor.encrypt("")

    assert encryptor.decrypt(blob, version) == ""


def test_unicode_survives(encryptor: Encryptor) -> None:
    text = "dor no ombro — RPE 8½, ficou puxado 💪"
    blob, version = encryptor.encrypt(text)

    assert encryptor.decrypt(blob, version) == text


def test_json_helpers_round_trip(encryptor: Encryptor) -> None:
    """athlete_profile.injuries and raw_message.payload are JSON serialised
    before encryption (§22.2)."""
    payload = {"region": "ombro_direito", "since": "2026-03", "severity": None}

    blob, version = encryptor.encrypt_json(payload)

    assert encryptor.decrypt_json(blob, version) == payload


def test_keyring_rejects_a_key_of_the_wrong_size() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        KeyRing({1: b"short"}, current_version=1)


def test_keyring_requires_the_current_version_to_exist() -> None:
    with pytest.raises(ValueError, match="current_version"):
        KeyRing({1: KEY_V1}, current_version=2)
