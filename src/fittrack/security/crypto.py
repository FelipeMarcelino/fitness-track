"""AES-256-GCM column encryption (spec 22.2).

Encrypted **before** the value reaches Postgres, so the database sees only
bytes. That is the third of the three layers in 22.1, and the one that protects
against the realistic scenario: a dump, a leaked backup, an operator or a
replica with more access than intended (AD-32).

**Blob format:** `version (2 bytes, big endian) || nonce (12) || ciphertext+tag`.

The version travels *inside* the blob so decryption never depends on a caller
passing the right one. The `key_version` column still exists, for a different
job — the rotation worker filters on it to find rows it has yet to rewrite — and
a disagreement between the two means a half-finished rotation, so it is an error
rather than something to read past.

**Associated data is mandatory.** AES-GCM authenticates it alongside the
ciphertext, which is what makes a byte-for-byte intact blob fail to decrypt in
any other row, column, table or tenant. Without it, a dump plus write access
would let anyone move one tenant's health report into another's.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fittrack.settings import Settings

# Two bytes, matching the positive SMALLINT the schema uses for `key_version`.
VERSION_BYTES: Final = 2
NONCE_BYTES: Final = 12
KEY_BYTES: Final = 32
MIN_VERSION: Final = 1
MAX_VERSION: Final = 32767

# Pinned in the AAD so a future change of scheme cannot be read by this one.
CONTRACT: Final = b"fittrack:v1"


class KeyringError(ValueError):
    """The keyring does not satisfy the contract of section 22.2."""


class DecryptionError(Exception):
    """A blob did not authenticate.

    Deliberately one exception with one message for every cause — tampering, an
    unknown version, the wrong key, the wrong associated data. Distinguishing
    them for the caller would also distinguish them for an attacker, which is a
    padding oracle by another name.
    """


_FAILED = "could not decrypt: the value did not authenticate"


@dataclass(frozen=True)
class Keyring:
    """The versioned master keys, and which one new writes use.

    `keys` is never printed: `repr` is suppressed on the field so a keyring in a
    traceback or a log line shows its versions and nothing else.
    """

    keys: Mapping[int, bytes] = field(repr=False)
    active_version: int

    def __post_init__(self) -> None:
        if not self.keys:
            raise KeyringError("the keyring is empty")
        for version, key in self.keys.items():
            if not MIN_VERSION <= version <= MAX_VERSION:
                raise KeyringError(f"key version {version} is outside {MIN_VERSION}..{MAX_VERSION}")
            if len(key) != KEY_BYTES:
                # The length, never the material.
                raise KeyringError(f"key {version} must be {KEY_BYTES} bytes")
        if self.active_version not in self.keys:
            raise KeyringError(f"active key version {self.active_version} is not in the keyring")

    @classmethod
    def from_settings(cls, settings: Settings) -> Keyring:
        """Build from an already-validated environment.

        `Settings` has parsed the JSON, decoded the base64, bound-checked every
        version and confirmed the active one exists. Re-deciding any of that
        here would create a second opinion about what a valid keyring is.
        """
        return cls(keys=settings.encryption_keys, active_version=settings.active_key_version)

    @property
    def versions(self) -> frozenset[int]:
        return frozenset(self.keys)

    def assert_can_read(self, versions_present: set[int]) -> None:
        """Every version still in the database must have its key here.

        Called before a deployment starts serving: a missing key does not fail
        until something reads a row that uses it, which could be months later.
        """
        missing = sorted(versions_present - self.versions)
        if missing:
            raise KeyringError(
                f"rows exist under key version(s) {missing}, which are unreadable with this keyring"
            )

    def assert_retirable(self, version: int, versions_present: set[int]) -> None:
        """A key may only be dropped once nothing in the database uses it.

        Dropping one early does not degrade anything gracefully — every row on
        that version becomes permanently unreadable.
        """
        if version == self.active_version:
            raise KeyringError(f"key version {version} is active and cannot be retired")
        if version in versions_present:
            raise KeyringError(f"key version {version} is still in use by rows in the database")


def column_aad(*, tenant_id: int, table: str, column: str, row_id: int) -> bytes:
    """Associated data for a domain column.

    The row id is part of it, so the repository has to reserve the `BIGSERIAL`
    before encrypting — the ordering the spec calls for. Without the id, a blob
    would move freely between rows of the same column.
    """
    return b"|".join(
        [
            CONTRACT,
            table.encode(),
            column.encode(),
            f"tenant:{tenant_id}".encode(),
            f"row:{row_id}".encode(),
        ]
    )


def identity_aad(*, channel: str, external_id_hash: bytes) -> bytes:
    """Associated data for `channel_identity.external_id` — the pre-tenant case.

    It names neither a tenant nor a row id, and cannot: the first webhook knows
    only a channel and a hash, and the tenant and its identity are created in
    one transaction. The hash pins it to a single identity all the same, which
    is what the row id does everywhere else.
    """
    return b"|".join(
        [
            CONTRACT,
            b"channel_identity",
            b"external_id",
            f"channel:{channel}".encode(),
            f"hash:{external_id_hash.hex()}".encode(),
        ]
    )


def version_of(blob: bytes) -> int:
    """The key version recorded inside a blob."""
    if len(blob) < VERSION_BYTES:
        raise DecryptionError(_FAILED)
    return int.from_bytes(blob[:VERSION_BYTES], "big")


class ColumnCipher:
    """Encrypts with the active key; decrypts with whichever the blob names."""

    def __init__(self, keyring: Keyring) -> None:
        self._keyring = keyring

    @property
    def active_version(self) -> int:
        """What a fresh write records, and what a rewritten row becomes."""
        return self._keyring.active_version

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        version = self._keyring.active_version
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._keyring.keys[version]).encrypt(nonce, plaintext, aad)
        return version.to_bytes(VERSION_BYTES, "big") + nonce + sealed

    def decrypt(self, blob: bytes, aad: bytes, declared_version: int | None = None) -> bytes:
        """Decrypt, optionally checking the blob against the row's `key_version`.

        Pass `declared_version` where the column is available. The two agreeing
        is the normal case; disagreeing means a rotation stopped halfway, and
        reading past it would hide that.
        """
        if len(blob) <= VERSION_BYTES + NONCE_BYTES:
            raise DecryptionError(_FAILED)

        version = int.from_bytes(blob[:VERSION_BYTES], "big")
        if declared_version is not None and declared_version != version:
            raise DecryptionError(
                f"key_version column says {declared_version} and the blob says {version}: "
                "a rotation did not finish"
            )

        key = self._keyring.keys.get(version)
        if key is None:
            raise DecryptionError(_FAILED)

        nonce = blob[VERSION_BYTES : VERSION_BYTES + NONCE_BYTES]
        try:
            return AESGCM(key).decrypt(nonce, blob[VERSION_BYTES + NONCE_BYTES :], aad)
        except InvalidTag:
            # `from None`: the cause would say which check failed, and telling
            # the caller apart from telling an attacker is not possible here.
            raise DecryptionError(_FAILED) from None
