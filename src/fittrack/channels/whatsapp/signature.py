"""X-Hub-Signature-256 verification (§18.2).

The webhook endpoint is public by necessity, so this is the only thing between
the internet and the queue.
"""

from __future__ import annotations

import hashlib
import hmac

PREFIX = "sha256="
DIGEST_HEX_LENGTH = 64


def verify_signature(body: bytes, header: str | None, app_secret: bytes) -> bool:
    """True when `header` is Meta's HMAC-SHA256 of exactly these bytes.

    Takes the raw body, never a re-serialised object: re-encoding JSON changes
    whitespace and key order, and the digest is over the bytes that arrived.

    Returns False for anything malformed rather than raising. An unhandled
    error here is a 500, and Meta disables endpoints that keep failing.
    """
    if not header or not header.startswith(PREFIX):
        return False

    received = header[len(PREFIX) :]
    if len(received) != DIGEST_HEX_LENGTH:
        return False

    expected = hmac.new(app_secret, body, hashlib.sha256).hexdigest()
    # compare_digest, not ==: byte-by-byte comparison leaks the correct digest
    # through timing, one byte at a time.
    return hmac.compare_digest(received, expected)
