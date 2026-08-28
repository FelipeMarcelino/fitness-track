"""The searchable identity hash (spec 22.2).

`channel_identity.external_id` is encrypted with a random nonce, so
`WHERE external_id = ?` cannot work — and every webhook begins with "whose
account is this?". This HMAC is what turns that into one index probe instead of
a table scan that decrypts row by row.

**The cost, stated because the spec states it.** A deterministic hash is open to
enumeration by anyone holding the pepper *and* knowing the input space, and a
Telegram `chat.id` has few significant digits. The mitigation is that the pepper
lives in the environment and never in the database, so a dump alone — the
adversary AD-32 has in view — cannot reverse it. A compromised machine defeats
this, and equally defeats the encryption key.

**Why the pepper does not rotate dual-read.** Hashes under two peppers would
both escape the uniqueness index, so the same account could resolve to two
tenants. Rotation is an atomic maintenance instead (see `rotate_pepper`).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

from pydantic import SecretStr

HASH_BYTES: Final = 32

# A pepper shorter than the digest adds no work for an attacker who already has
# the input space; the spec treats it as key material, so it is sized like key
# material.
MIN_PEPPER_BYTES: Final = 32


class PepperError(ValueError):
    """The identity pepper does not satisfy the contract of section 22.2."""


def load_pepper(secret: SecretStr) -> bytes:
    """The pepper as bytes, checked once, so callers cannot pass a short one."""
    pepper = secret.get_secret_value().encode()
    _require_pepper(pepper)
    return pepper


def identity_hash(channel: str, external_id: str, pepper: bytes) -> bytes:
    """A stable, indexable digest of one account on one channel.

    The channel is part of the input because the identifiers live in different
    namespaces: a Telegram `chat.id` and a WhatsApp BSUID could be the same
    string and are not the same account (spec 1.3).
    """
    _require_pepper(pepper)
    if not channel:
        # Never the identifier itself in a message: it is the value that
        # correlates a person outside the product (spec 22, "correlação").
        raise ValueError("channel must not be empty")
    if not external_id:
        raise ValueError("external_id must not be empty")

    # Length-prefixed, not separator-joined: with a separator, ("telegram",
    # "x|y") and ("telegram|x", "y") would hash to the same value, and a
    # crafted identifier could impersonate another channel's account.
    message = b"".join(
        len(part).to_bytes(4, "big") + part for part in (channel.encode(), external_id.encode())
    )
    return hmac.new(pepper, message, hashlib.sha256).digest()


def _require_pepper(pepper: bytes) -> None:
    if len(pepper) < MIN_PEPPER_BYTES:
        # The length, never the value.
        raise PepperError(f"FITTRACK_IDENTITY_PEPPER must be at least {MIN_PEPPER_BYTES} bytes")
