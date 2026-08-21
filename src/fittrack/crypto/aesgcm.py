"""AES-256-GCM for the columns listed in §22.2 of doc/spec.md.

Encryption happens in the application, before the value reaches Postgres, so a
database dump or a leaked backup carries ciphertext. This is the layer that
protects against the realistic scenario; TLS and disk encryption protect
against the machine being stolen.

GCM is authenticated: a wrong key or a tampered byte raises instead of
returning plausible garbage, which matters because garbage would flow straight
into a health record or an analysis.
"""

from __future__ import annotations

import json
import os
from typing import Any, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES: Final = 32  # AES-256
NONCE_BYTES: Final = 12  # GCM standard; 96 bits is what the mode is built for
VERSION_BYTES: Final = 2  # big-endian key version, prefixed to every blob
MIN_BLOB_BYTES: Final = VERSION_BYTES + NONCE_BYTES + 16  # + GCM tag


class DecryptionError(Exception):
    """Raised when a value cannot be decrypted and verified.

    Never swallowed into a default: a health report that silently decrypts to
    nothing is worse than an error, because nobody notices.
    """


class KeyRing:
    """The keys currently in play.

    More than one because §22.2 rotates progressively: new writes take the new
    key while a background job rewrites history. Every version still present in
    the table must stay readable, or rotation becomes an outage.
    """

    def __init__(self, keys: dict[int, bytes], current_version: int) -> None:
        for version, key in keys.items():
            if len(key) != KEY_BYTES:
                raise ValueError(
                    f"key version {version} must be {KEY_BYTES} bytes for "
                    f"AES-256-GCM, got {len(key)}"
                )
        if current_version not in keys:
            raise ValueError(f"current_version {current_version} has no key in the ring")
        self._keys = dict(keys)
        self.current_version = current_version

    def get(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError as exc:
            raise DecryptionError(
                f"no key for key version {version}; the row predates this "
                f"deployment or the ring lost a key"
            ) from exc

    @property
    def current(self) -> bytes:
        return self._keys[self.current_version]


class Encryptor:
    """Encrypts with the ring's current key, decrypts with whichever the row
    was written under."""

    def __init__(self, keyring: KeyRing) -> None:
        self._ring = keyring

    @property
    def current_version(self) -> int:
        """The version new writes are stamped with."""
        return self._ring.current_version

    def encrypt(self, plaintext: str) -> tuple[bytes, int]:
        """Returns the stored blob and the key version that produced it.

        Layout: version (2 bytes, big endian) || nonce (12) || ciphertext+tag.
        The version travels inside the blob so decryption never depends on a
        caller passing the right one. The key_version column still exists,
        for a different job: it is what the rotation query filters on to find
        rows that still need rewriting.

        The nonce is random per call and prefixed to the ciphertext. A fixed or
        counter nonce would make ciphertext deterministic, and then anyone with
        read access could tell which rows share a value -- how many users
        reported the same injury, say -- without decrypting anything. Under GCM
        a repeated nonce is worse than that: it breaks the mode outright.
        """
        version = self._ring.current_version
        nonce = os.urandom(NONCE_BYTES)
        cipher = AESGCM(self._ring.current)
        body = cipher.encrypt(nonce, plaintext.encode("utf-8"), None)
        return version.to_bytes(VERSION_BYTES, "big") + nonce + body, version

    def decrypt(self, blob: bytes, key_version: int | None = None) -> str:
        """`key_version` is accepted for callers that hold the column value,
        but the blob's own prefix wins: a row written under key 1 must decrypt
        after the ring advances to 2, and trusting the caller made that fail.
        """
        if len(blob) < MIN_BLOB_BYTES:
            raise DecryptionError(
                f"blob is {len(blob)} bytes, too short to be a valid value; "
                f"the column is truncated or corrupt"
            )

        embedded = int.from_bytes(blob[:VERSION_BYTES], "big")
        if key_version is not None and key_version != embedded:
            raise DecryptionError(
                f"key version mismatch: column says {key_version}, blob says "
                f"{embedded}; the row was rewritten without updating the column"
            )

        key = self._ring.get(embedded)
        nonce = blob[VERSION_BYTES : VERSION_BYTES + NONCE_BYTES]
        ciphertext = blob[VERSION_BYTES + NONCE_BYTES :]
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise DecryptionError(
                "ciphertext failed authentication: wrong key or tampered value"
            ) from exc
        except ValueError as exc:
            # AESGCM raises ValueError, not InvalidTag, for a malformed nonce.
            raise DecryptionError(f"malformed ciphertext: {exc}") from exc
        return plaintext.decode("utf-8")

    def encrypt_json(self, value: Any) -> tuple[bytes, int]:
        """For athlete_profile.injuries and raw_message.payload, which are JSON
        before they are ciphertext (§22.2).

        sort_keys because a stable serialisation makes ciphertext comparable
        across rewrites during a rotation.
        """
        return self.encrypt(json.dumps(value, sort_keys=True, ensure_ascii=False))

    def decrypt_json(self, blob: bytes, key_version: int | None = None) -> Any:
        return json.loads(self.decrypt(blob, key_version))
