"""The searchable identity hash (spec 22.2).

`external_id` is encrypted with a random nonce, so `WHERE external_id = ?` is
impossible — and every webhook starts with "whose account is this?". The hash
is what makes that lookup a single index probe instead of a full-table scan
that decrypts row by row.

The cost is real and stated in the spec: a deterministic hash is open to
enumeration by anyone holding the pepper *and* the input space. The mitigation
is that the pepper lives in the environment, never in the database — so a
database dump alone, which is the adversary AD-32 has in view, cannot reverse it.
"""

from __future__ import annotations

import pytest

from fittrack.security.identity_hash import (
    HASH_BYTES,
    PepperError,
    identity_hash,
    load_pepper,
)

PEPPER = b"a-development-pepper-of-sufficient-length"
OTHER = b"a-different-pepper-of-sufficient-length!"


def test_the_hash_is_deterministic() -> None:
    """Without this there is no lookup at all."""
    first = identity_hash("telegram", "123456789", PEPPER)
    second = identity_hash("telegram", "123456789", PEPPER)
    assert first == second


def test_the_hash_is_a_full_sha256() -> None:
    assert len(identity_hash("telegram", "1", PEPPER)) == HASH_BYTES == 32


def test_the_hash_does_not_contain_the_identifier() -> None:
    assert b"123456789" not in identity_hash("telegram", "123456789", PEPPER)


def test_the_channel_is_part_of_the_input() -> None:
    """A Telegram chat.id and a WhatsApp bsuid could collide as bare strings."""
    assert identity_hash("telegram", "1", PEPPER) != identity_hash("whatsapp", "1", PEPPER)


def test_different_identifiers_hash_differently() -> None:
    assert identity_hash("telegram", "1", PEPPER) != identity_hash("telegram", "2", PEPPER)


def test_the_pepper_changes_every_hash() -> None:
    """Why rotation is a maintenance window and not a dual-read (spec 22.2)."""
    assert identity_hash("telegram", "1", PEPPER) != identity_hash("telegram", "1", OTHER)


def test_the_separator_cannot_be_smuggled_across_fields() -> None:
    """`("telegram", "x|y")` and `("telegram|x", "y")` must not collide."""
    assert identity_hash("telegram", "x|y", PEPPER) != identity_hash("telegram|x", "y", PEPPER)


def test_an_empty_identifier_is_refused() -> None:
    with pytest.raises(ValueError, match="external_id"):
        identity_hash("telegram", "", PEPPER)


def test_an_empty_channel_is_refused() -> None:
    with pytest.raises(ValueError, match="channel"):
        identity_hash("", "1", PEPPER)


# --------------------------------------------------------------------------- #
# The pepper
# --------------------------------------------------------------------------- #


def test_a_short_pepper_is_refused() -> None:
    """A guessable pepper makes the hash reversible for a small input space."""
    with pytest.raises(PepperError, match="32"):
        identity_hash("telegram", "1", b"too-short")


def test_an_empty_pepper_is_refused() -> None:
    with pytest.raises(PepperError):
        identity_hash("telegram", "1", b"")


def test_the_pepper_loads_from_a_secret() -> None:
    from pydantic import SecretStr

    assert load_pepper(SecretStr(PEPPER.decode())) == PEPPER


def test_an_error_never_quotes_the_pepper() -> None:
    with pytest.raises(PepperError) as raised:
        identity_hash("telegram", "1", b"short-pepper")
    assert "short-pepper" not in str(raised.value)


def test_an_error_never_quotes_the_identifier() -> None:
    """The `external_id` is the value that correlates a person outside the product."""
    with pytest.raises(ValueError) as raised:
        identity_hash("", "123456789", PEPPER)
    assert "123456789" not in str(raised.value)
