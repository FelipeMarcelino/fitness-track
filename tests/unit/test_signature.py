"""Webhook signature verification (§18.2).

The signature is the only thing standing between the internet and the queue:
the endpoint is public by necessity, so anyone can POST to it.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from fittrack.channels.whatsapp.signature import verify_signature

SECRET = b"app-secret"
BODY = b'{"object":"whatsapp_business_account"}'


def _sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_accepts_a_correct_signature() -> None:
    assert verify_signature(BODY, _sign(BODY), SECRET)


def test_rejects_a_body_that_was_altered() -> None:
    assert not verify_signature(BODY + b" ", _sign(BODY), SECRET)


def test_rejects_a_signature_made_with_another_secret() -> None:
    assert not verify_signature(BODY, _sign(BODY, b"someone-elses"), SECRET)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "deadbeef",  # no algorithm prefix
        "sha1=" + hmac.new(SECRET, BODY, hashlib.sha1).hexdigest(),  # wrong algo
        "sha256=",  # empty digest
        "sha256=nothex",
        "sha256=" + "a" * 63,  # short digest
    ],
)
def test_rejects_malformed_headers(header: str) -> None:
    """A malformed header must be a rejection, not an exception: an unhandled
    error here is a 500, and Meta disables endpoints that keep failing."""
    assert not verify_signature(BODY, header, SECRET)


def test_rejects_a_missing_header() -> None:
    assert not verify_signature(BODY, None, SECRET)


def test_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte-by-byte comparison leaks the correct digest through timing. This
    asserts the implementation calls compare_digest rather than ==."""
    called: list[bool] = []
    original = hmac.compare_digest

    def _spy(a: object, b: object) -> bool:
        called.append(True)
        return original(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr("fittrack.channels.whatsapp.signature.hmac.compare_digest", _spy)
    verify_signature(BODY, _sign(BODY), SECRET)

    assert called, "signature comparison must use hmac.compare_digest"
